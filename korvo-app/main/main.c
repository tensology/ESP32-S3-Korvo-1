#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <errno.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "esp_http_server.h"
#include "esp_system.h"
#include "esp_random.h"
#include "cJSON.h"
#include "mdns.h"
#include "led_strip.h"
#include "esp_board_init.h"
#include "korvo_config.h"
#include "nvs.h"
#if CONFIG_BT_ENABLED
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_gap_ble_api.h"
#include "esp_gap_bt_api.h"
#include "esp_bt_device.h"
#include "esp_a2dp_api.h"
#include "esp_avrc_api.h"
#endif

#define AUDIO_RINGBUF_SIZE (256 * 1024)
#define TICKS_AT_LEAST_1(ms) ((pdMS_TO_TICKS(ms) > 0) ? pdMS_TO_TICKS(ms) : 1)
#define AUDIO_SAMPLE_RATE 16000
#define AUDIO_BIT_DEPTH 16
#define AUDIO_CHUNK_SIZE 640
#define AUDIO_CHUNK_BYTES ((size_t)AUDIO_CHUNK_SIZE * sizeof(int16_t))
/* ES7210 + I2S int32 stereo: energy is in the upper int32 range; scale before int16 clamp.
 * RECORD_VOLUME in board header sets PGA only. Increase shift if still clipped; decrease if too quiet. */
#ifndef KORVO_MIC_I32_MONO_SHIFT
#define KORVO_MIC_I32_MONO_SHIFT 12
#endif

static RingbufHandle_t audio_ringbuf = NULL;
static volatile bool stream_client_connected = false;
static SemaphoreHandle_t stream_mutex = NULL;
static RingbufHandle_t playback_ringbuf_local = NULL;
static RingbufHandle_t playback_ringbuf_bt = NULL;
#define PLAYBACK_RINGBUF_SIZE (256 * 1024)
#define PLAYBACK_PUSH_MAX (4096)
#define PLAYBACK_FRAME_BYTES (640)          /* 20 ms @ 16 kHz mono s16le input */
#define PLAYBACK_STEREO_FRAME_BYTES (1280)  /* 20 ms @ 16 kHz stereo s16le output */
#define PLAYBACK_PREBUFFER_BYTES (12800)    /* ~400 ms startup jitter buffer (lower latency, still jitter-tolerant) */
#define PLAYBACK_FIFO_BYTES (32768)
static volatile uint32_t inject_playback_deadline_ms = 0;
/* Inject telemetry (best-effort, lock-free counters for host-side pacing). */
static volatile uint32_t inject_http_posts_total = 0;
static volatile uint32_t inject_http_posts_dropped = 0;
static volatile uint32_t inject_http_bytes_accepted = 0;
static volatile uint32_t inject_playback_bytes_consumed = 0;
static volatile uint32_t inject_playback_underruns = 0;
static volatile uint32_t inject_playback_fifo_level = 0;

static const char *TAG = "KORVO";

#define LED_GPIO    33
#define LED_COUNT   12
#define LED_GLOBAL_BRIGHTNESS_CAP ((uint8_t)115) /* ~45% of 255 */

static volatile struct {
    bool on;
    int preset;
    uint8_t r, g, b;
    uint8_t brightness;
    int auto_off_ms;
    uint8_t pix[LED_COUNT][3];
} led_state = {0};

static portMUX_TYPE led_state_mux = portMUX_INITIALIZER_UNLOCKED;

/* When non-zero, breath cue (preset 4) runs until this timestamp (esp_log ms), then LEDs go off. */
static volatile uint32_t led_cue_until_ms;

/* ~2.6s ≈ one full inhale/exhale at 25ms/tick (anim_breathe). */
#define LED_CUE_BREATH_MS 2600U

static led_strip_handle_t strip = NULL;

static void led_start_breath_cue(uint8_t r, uint8_t g, uint8_t b, uint32_t duration_ms);

#if CONFIG_BT_ENABLED
#define BT_MAX_DEVICES 40
typedef struct {
    bool used;
    char addr[18];
    char name[64];
    int rssi;
    uint32_t last_seen_ms;
} bt_device_t;

static bt_device_t bt_devices[BT_MAX_DEVICES];
static bool bt_finding = false;
static bool bt_ready = false;
static bool bt_connected = false;
static bool bt_connecting = false;
static bool bt_a2dp_ready = false;
static bool bt_auto_reconnect = true;
static uint32_t bt_disc_res_count = 0;
static uint32_t bt_disc_start_count = 0;
static uint32_t bt_disc_stop_count = 0;
static uint32_t bt_disc_last_event_ms = 0;
static char bt_connected_addr[18] = {0};
static char bt_connected_name[64] = {0};
static bool bt_output_route_active = false; /* true => use BT output route, disable onboard output route */
static char bt_preferred_addr[18] = {0};
static char bt_preferred_name[64] = {0};
static portMUX_TYPE bt_mux = portMUX_INITIALIZER_UNLOCKED;
static esp_err_t bt_set_finding(bool on);
static void bt_set_connected_device(const char *addr, const char *name);
static void bt_clear_connected_device(void);
static esp_err_t bt_request_connect_device(const char *addr, const char *name);

typedef struct {
    char addr[18];
    char name[64];
} bt_connect_req_t;

static void bt_connect_worker_task(void *arg) {
    bt_connect_req_t *req = (bt_connect_req_t *)arg;
    if (!req) {
        vTaskDelete(NULL);
        return;
    }
    /* Connecting while inquiry scan is active can stall the BT stack and make HTTP appear dead.
       Stop scan first in worker context, then start the connection attempt. */
    if (bt_finding) {
        esp_err_t stop_e = bt_set_finding(false);
        if (stop_e != ESP_OK) {
            ESP_LOGW(TAG, "BT connect worker: failed to stop discovery first: %s", esp_err_to_name(stop_e));
        }
        vTaskDelay(pdMS_TO_TICKS(150));
    }
    esp_err_t e = bt_request_connect_device(req->addr, req->name);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "BT connect worker: connect start failed: %s", esp_err_to_name(e));
        portENTER_CRITICAL(&bt_mux);
        bt_connecting = false;
        portEXIT_CRITICAL(&bt_mux);
    }
    free(req);
    vTaskDelete(NULL);
}
static void bt_pref_load(void) {
    nvs_handle_t nvs = 0;
    if (nvs_open("bt_pref", NVS_READONLY, &nvs) != ESP_OK) return;
    size_t a_len = sizeof(bt_preferred_addr);
    size_t n_len = sizeof(bt_preferred_name);
    nvs_get_str(nvs, "addr", bt_preferred_addr, &a_len);
    nvs_get_str(nvs, "name", bt_preferred_name, &n_len);
    uint8_t auto_flag = 1;
    if (nvs_get_u8(nvs, "auto", &auto_flag) == ESP_OK) bt_auto_reconnect = (auto_flag != 0);
    nvs_close(nvs);
}

static void bt_pref_save(const char *addr, const char *name) {
    nvs_handle_t nvs = 0;
    if (nvs_open("bt_pref", NVS_READWRITE, &nvs) != ESP_OK) return;
    nvs_set_str(nvs, "addr", addr ? addr : "");
    nvs_set_str(nvs, "name", name ? name : "");
    nvs_set_u8(nvs, "auto", bt_auto_reconnect ? 1 : 0);
    nvs_commit(nvs);
    nvs_close(nvs);
}

static void bt_addr_to_str(const uint8_t *bda, char out[18]) {
    snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
             bda[0], bda[1], bda[2], bda[3], bda[4], bda[5]);
}

static bool bt_str_to_addr(const char *in, esp_bd_addr_t out) {
    if (!in || strlen(in) < 17) return false;
    unsigned int b[6];
    if (sscanf(in, "%02x:%02x:%02x:%02x:%02x:%02x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) return false;
    for (int i = 0; i < 6; i++) out[i] = (uint8_t)b[i];
    return true;
}

static int bt_find_index_by_addr(const char *addr) {
    for (int i = 0; i < BT_MAX_DEVICES; i++) {
        if (bt_devices[i].used && strncmp(bt_devices[i].addr, addr, sizeof(bt_devices[i].addr)) == 0) {
            return i;
        }
    }
    return -1;
}

static int bt_find_free_index(void) {
    for (int i = 0; i < BT_MAX_DEVICES; i++) {
        if (!bt_devices[i].used) return i;
    }
    int oldest_i = 0;
    uint32_t oldest_ms = UINT32_MAX;
    for (int i = 0; i < BT_MAX_DEVICES; i++) {
        if (bt_devices[i].last_seen_ms < oldest_ms) {
            oldest_ms = bt_devices[i].last_seen_ms;
            oldest_i = i;
        }
    }
    return oldest_i;
}

static void bt_update_seen_classic(const uint8_t bda[6], const char *name_in, int rssi_in) {
    char addr[18] = {0};
    bt_addr_to_str(bda, addr);
    char name[64] = {0};
    if (name_in && name_in[0] != '\0') {
        size_t n = strlen(name_in);
        if (n >= sizeof(name)) n = sizeof(name) - 1;
        memcpy(name, name_in, n);
        name[n] = '\0';
    }
    portENTER_CRITICAL(&bt_mux);
    int idx = bt_find_index_by_addr(addr);
    if (idx < 0) idx = bt_find_free_index();
    bt_devices[idx].used = true;
    strncpy(bt_devices[idx].addr, addr, sizeof(bt_devices[idx].addr) - 1);
    bt_devices[idx].addr[sizeof(bt_devices[idx].addr) - 1] = '\0';
    if (name[0] != '\0') {
        strncpy(bt_devices[idx].name, name, sizeof(bt_devices[idx].name) - 1);
        bt_devices[idx].name[sizeof(bt_devices[idx].name) - 1] = '\0';
    }
    bt_devices[idx].rssi = rssi_in;
    bt_devices[idx].last_seen_ms = esp_log_timestamp();
    bool should_auto_connect = false;
    if (bt_auto_reconnect && bt_finding && !bt_connected && !bt_connecting && bt_preferred_addr[0] != '\0' &&
        strncmp(bt_preferred_addr, addr, sizeof(bt_preferred_addr)) == 0) {
        should_auto_connect = true;
    }
    portEXIT_CRITICAL(&bt_mux);
    if (should_auto_connect) {
        bt_set_finding(false);
        bt_request_connect_device(addr, name);
    }
}

static void bt_gap_bt_cb(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param) {
    switch (event) {
        case ESP_BT_GAP_DISC_RES_EVT:
            if (param) {
                char name[64] = {0};
                int rssi = -127;
                const uint8_t *eir = NULL;
                for (int i = 0; i < param->disc_res.num_prop; i++) {
                    const esp_bt_gap_dev_prop_t *p = &param->disc_res.prop[i];
                    if (p->type == ESP_BT_GAP_DEV_PROP_BDNAME && p->val && p->len > 0) {
                        int n = p->len;
                        if (n >= (int)sizeof(name)) n = (int)sizeof(name) - 1;
                        memcpy(name, p->val, (size_t)n);
                        name[n] = '\0';
                    } else if (p->type == ESP_BT_GAP_DEV_PROP_RSSI && p->val && p->len >= (int)sizeof(int8_t)) {
                        rssi = (int)(*(int8_t *)p->val);
                    } else if (p->type == ESP_BT_GAP_DEV_PROP_EIR && p->val && p->len > 0) {
                        eir = (const uint8_t *)p->val;
                    }
                }
                /* Many Classic BT devices only expose local name via EIR, not BDNAME. */
                if (name[0] == '\0' && eir) {
                    uint8_t eir_len = 0;
                    uint8_t *eir_name = esp_bt_gap_resolve_eir_data((uint8_t *)eir, ESP_BT_EIR_TYPE_CMPL_LOCAL_NAME, &eir_len);
                    if (!eir_name || eir_len == 0) {
                        eir_name = esp_bt_gap_resolve_eir_data((uint8_t *)eir, ESP_BT_EIR_TYPE_SHORT_LOCAL_NAME, &eir_len);
                    }
                    if (eir_name && eir_len > 0) {
                        int n = (int)eir_len;
                        if (n >= (int)sizeof(name)) n = (int)sizeof(name) - 1;
                        memcpy(name, eir_name, (size_t)n);
                        name[n] = '\0';
                    }
                }
                bt_disc_res_count++;
                bt_disc_last_event_ms = esp_log_timestamp();
                if (bt_disc_res_count <= 5 || (bt_disc_res_count % 20u) == 0u) {
                    char addr[18] = {0};
                    bt_addr_to_str(param->disc_res.bda, addr);
                    ESP_LOGI(TAG, "BT DISC_RES #%u addr=%s rssi=%d name='%s'",
                             (unsigned)bt_disc_res_count, addr, rssi, name);
                }
                bt_update_seen_classic(param->disc_res.bda, name, rssi);
            }
            break;
        case ESP_BT_GAP_DISC_STATE_CHANGED_EVT:
            if (param && param->disc_st_chg.state == ESP_BT_GAP_DISCOVERY_STOPPED) {
                bt_disc_stop_count++;
                bt_disc_last_event_ms = esp_log_timestamp();
                ESP_LOGI(TAG, "BT discovery stopped (#%u), keep_finding=%d",
                         (unsigned)bt_disc_stop_count, (int)bt_finding);
                bool keep_finding = false;
                portENTER_CRITICAL(&bt_mux);
                keep_finding = bt_finding;
                portEXIT_CRITICAL(&bt_mux);
                if (keep_finding) {
                    bt_disc_start_count++;
                    esp_bt_gap_start_discovery(ESP_BT_INQ_MODE_GENERAL_INQUIRY, 8, 0);
                    ESP_LOGI(TAG, "BT discovery restarted (#%u)", (unsigned)bt_disc_start_count);
                }
            }
            break;
        default:
            break;
    }
}

static esp_err_t bt_set_finding(bool on) {
    if (!bt_ready) return ESP_ERR_INVALID_STATE;
    if (on == bt_finding) return ESP_OK;
    if (on) {
        bt_disc_start_count++;
        bt_disc_last_event_ms = esp_log_timestamp();
        esp_err_t e = esp_bt_gap_start_discovery(ESP_BT_INQ_MODE_GENERAL_INQUIRY, 8, 0);
        if (e != ESP_OK) return e;
        bt_finding = true;
        ESP_LOGI(TAG, "BT finding enabled (start #%u)", (unsigned)bt_disc_start_count);
        return ESP_OK;
    }
    esp_err_t e = esp_bt_gap_cancel_discovery();
    bt_finding = false;
    ESP_LOGI(TAG, "BT finding disabled");
    return e;
}

static void bt_set_preferred_device(const char *addr, const char *name) {
    portENTER_CRITICAL(&bt_mux);
    strncpy(bt_preferred_addr, addr ? addr : "", sizeof(bt_preferred_addr) - 1);
    bt_preferred_addr[sizeof(bt_preferred_addr) - 1] = '\0';
    strncpy(bt_preferred_name, name ? name : "", sizeof(bt_preferred_name) - 1);
    bt_preferred_name[sizeof(bt_preferred_name) - 1] = '\0';
    portEXIT_CRITICAL(&bt_mux);
    bt_pref_save(bt_preferred_addr, bt_preferred_name);
}

static void bt_clear_preferred_device(void) {
    bt_set_preferred_device("", "");
}

static void bt_set_connected_device(const char *addr, const char *name) {
    portENTER_CRITICAL(&bt_mux);
    bt_connected = true;
    bt_connecting = false;
    bt_output_route_active = true;
    strncpy(bt_connected_addr, addr ? addr : "", sizeof(bt_connected_addr) - 1);
    bt_connected_addr[sizeof(bt_connected_addr) - 1] = '\0';
    strncpy(bt_connected_name, name ? name : "", sizeof(bt_connected_name) - 1);
    bt_connected_name[sizeof(bt_connected_name) - 1] = '\0';
    strncpy(bt_preferred_addr, bt_connected_addr, sizeof(bt_preferred_addr) - 1);
    bt_preferred_addr[sizeof(bt_preferred_addr) - 1] = '\0';
    strncpy(bt_preferred_name, bt_connected_name, sizeof(bt_preferred_name) - 1);
    bt_preferred_name[sizeof(bt_preferred_name) - 1] = '\0';
    portEXIT_CRITICAL(&bt_mux);
    bt_pref_save(bt_preferred_addr, bt_preferred_name);
    /* Momentary purple breath — sustained BT "link" is not shown on the ring. */
    led_start_breath_cue(168, 40, 255, LED_CUE_BREATH_MS);
}

static void bt_clear_connected_device(void) {
    portENTER_CRITICAL(&bt_mux);
    bt_connected = false;
    bt_connecting = false;
    bt_output_route_active = false; /* fallback to onboard output route */
    bt_connected_addr[0] = '\0';
    bt_connected_name[0] = '\0';
    portEXIT_CRITICAL(&bt_mux);
    led_start_breath_cue(168, 40, 255, LED_CUE_BREATH_MS);
}

static int32_t bt_a2dp_data_cb(uint8_t *data, int32_t len) {
    if (!data || len <= 0) return 0;
    memset(data, 0, (size_t)len);
    if (!playback_ringbuf_bt || !bt_output_route_active) return len;
    /* Input format pushed by UI: PCM16 mono 16kHz. A2DP callback expects stereo frames.
       We upsample by nearest-neighbor to fill requested callback bytes. */
    const int out_samples = len / 2; /* int16 count (interleaved stereo) */
    int16_t *out = (int16_t *)data;
    static int16_t mono_cache[PLAYBACK_PUSH_MAX];
    static int mono_count = 0;
    static float src_pos = 0.0f;
    const float step = 16000.0f / 44100.0f; /* source/sample ratio per output frame */
    for (int i = 0; i < out_samples; i += 2) {
        while (((int)src_pos) >= mono_count) {
            size_t item_size = 0;
            void *item = xRingbufferReceiveUpTo(playback_ringbuf_bt, &item_size, TICKS_AT_LEAST_1(2), PLAYBACK_PUSH_MAX * sizeof(int16_t));
            if (!item || item_size < sizeof(int16_t)) {
                out[i] = 0;
                out[i + 1] = 0;
                goto next_frame;
            }
            mono_count = (int)(item_size / sizeof(int16_t));
            if (mono_count > PLAYBACK_PUSH_MAX) mono_count = PLAYBACK_PUSH_MAX;
            memcpy(mono_cache, item, (size_t)mono_count * sizeof(int16_t));
            vRingbufferReturnItem(playback_ringbuf_bt, item);
            src_pos -= (float)((int)src_pos);
        }
        {
            int idx = (int)src_pos;
            if (idx < 0) idx = 0;
            if (idx >= mono_count) idx = mono_count - 1;
            int16_t s = mono_cache[idx];
            out[i] = s;
            out[i + 1] = s;
        }
next_frame:
        src_pos += step;
    }
    return len;
}

static void bt_a2dp_cb(esp_a2d_cb_event_t event, esp_a2d_cb_param_t *param) {
    switch (event) {
        case ESP_A2D_CONNECTION_STATE_EVT:
            if (param && param->conn_stat.state == ESP_A2D_CONNECTION_STATE_CONNECTED) {
                char addr[18] = {0};
                bt_addr_to_str(param->conn_stat.remote_bda, addr);
                bt_set_connected_device(addr, bt_connected_name);
            } else if (param && (
                       param->conn_stat.state == ESP_A2D_CONNECTION_STATE_DISCONNECTED ||
                       param->conn_stat.state == ESP_A2D_CONNECTION_STATE_DISCONNECTING)) {
                bt_clear_connected_device();
            } else if (param && param->conn_stat.state == ESP_A2D_CONNECTION_STATE_CONNECTING) {
                portENTER_CRITICAL(&bt_mux);
                bt_connecting = true;
                portEXIT_CRITICAL(&bt_mux);
                led_start_breath_cue(168, 40, 255, LED_CUE_BREATH_MS);
            }
            break;
        case ESP_A2D_PROF_STATE_EVT:
            if (param) bt_a2dp_ready = (param->a2d_prof_stat.init_state == ESP_A2D_INIT_SUCCESS);
            break;
        default:
            break;
    }
}

static esp_err_t bt_request_connect_device(const char *addr, const char *name) {
    if (!bt_ready || !bt_a2dp_ready) return ESP_ERR_INVALID_STATE;
    esp_bd_addr_t bda = {0};
    if (!bt_str_to_addr(addr, bda)) return ESP_ERR_INVALID_ARG;
    portENTER_CRITICAL(&bt_mux);
    bt_connecting = true;
    if (name && *name) {
        strncpy(bt_connected_name, name, sizeof(bt_connected_name) - 1);
        bt_connected_name[sizeof(bt_connected_name) - 1] = '\0';
    }
    portEXIT_CRITICAL(&bt_mux);
    return esp_a2d_source_connect(bda);
}
#endif

#if !CONFIG_BT_ENABLED
static bool bt_route_active(void) { return false; }
#endif

static void anim_clear(void) {
    if (strip) {
        led_strip_clear(strip);
        led_strip_refresh(strip);
    }
}

static void anim_solid(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    if (!strip) return;
    float s = bright / 255.0f;
    for (int i = 0; i < LED_COUNT; i++)
        led_strip_set_pixel(strip, i, (uint8_t)(r*s), (uint8_t)(g*s), (uint8_t)(b*s));
    led_strip_refresh(strip);
}

static void anim_spinner(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    static int pos = 0;
    if (!strip) return;
    float s = bright / 255.0f;
    led_strip_clear(strip);
    for (int i = 0; i < LED_COUNT; i++) {
        int dist = (i - pos + LED_COUNT) % LED_COUNT;
        if (dist <= 4) {
            float fade = (5 - dist) / 5.0f;
            led_strip_set_pixel(strip, i,
                (uint8_t)(r * fade * s), (uint8_t)(g * fade * s), (uint8_t)(b * fade * s));
        }
    }
    led_strip_refresh(strip);
    pos = (pos + 1) % LED_COUNT;
}

static void anim_spinner_reverse(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    static int pos = 0;
    if (!strip) return;
    float s = bright / 255.0f;
    led_strip_clear(strip);
    for (int i = 0; i < LED_COUNT; i++) {
        int dist = (pos - i + LED_COUNT) % LED_COUNT;
        if (dist <= 4) {
            float fade = (5 - dist) / 5.0f;
            led_strip_set_pixel(strip, i,
                (uint8_t)(r * fade * s), (uint8_t)(g * fade * s), (uint8_t)(b * fade * s));
        }
    }
    led_strip_refresh(strip);
    pos = (pos - 1 + LED_COUNT) % LED_COUNT;
}

static void anim_breathe(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    static int step = 0, dir = 1;
    if (!strip) return;
    float s = bright / 255.0f;
    float level = step / 100.0f;
    for (int i = 0; i < LED_COUNT; i++)
        led_strip_set_pixel(strip, i,
            (uint8_t)(r * level * s), (uint8_t)(g * level * s), (uint8_t)(b * level * s));
    led_strip_refresh(strip);
    step += dir * 2;
    if (step >= 100 || step <= 0) dir *= -1;
}

/* Adafruit-style color wheel (0–255) → RGB */
static void wheel_rgb(uint8_t pos, uint8_t *rp, uint8_t *gp, uint8_t *bp) {
    pos = (uint8_t)(255 - pos);
    if (pos < 85) {
        *rp = (uint8_t)(255 - pos * 3);
        *gp = 0;
        *bp = (uint8_t)(pos * 3);
    } else if (pos < 170) {
        pos = (uint8_t)(pos - 85);
        *rp = 0;
        *gp = (uint8_t)(pos * 3);
        *bp = (uint8_t)(255 - pos * 3);
    } else {
        pos = (uint8_t)(pos - 170);
        *rp = (uint8_t)(pos * 3);
        *gp = (uint8_t)(255 - pos * 3);
        *bp = 0;
    }
}

static void anim_rainbow(uint8_t bright) {
    static uint8_t off = 0;
    if (!strip) return;
    float s = bright / 255.0f;
    for (int i = 0; i < LED_COUNT; i++) {
        uint8_t r, g, b;
        wheel_rgb((uint8_t)(off + (uint8_t)(i * (256 / LED_COUNT))), &r, &g, &b);
        led_strip_set_pixel(strip, i, (uint8_t)(r * s), (uint8_t)(g * s), (uint8_t)(b * s));
    }
    led_strip_refresh(strip);
    off = (uint8_t)(off + 4);
}

static void anim_rainbow_reverse(uint8_t bright) {
    static uint8_t off = 0;
    if (!strip) return;
    float s = bright / 255.0f;
    for (int i = 0; i < LED_COUNT; i++) {
        uint8_t r, g, b;
        wheel_rgb((uint8_t)(off - (uint8_t)(i * (256 / LED_COUNT))), &r, &g, &b);
        led_strip_set_pixel(strip, i, (uint8_t)(r * s), (uint8_t)(g * s), (uint8_t)(b * s));
    }
    led_strip_refresh(strip);
    off = (uint8_t)(off - 4);
}

static void anim_chase(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    static int pos = 0;
    if (!strip) return;
    float s = bright / 255.0f;
    led_strip_clear(strip);
    for (int k = 0; k < 4; k++) {
        int i = (pos + k) % LED_COUNT;
        float fade = (4.0f - (float)k) / 4.0f;
        led_strip_set_pixel(strip, i,
            (uint8_t)(r * fade * s), (uint8_t)(g * fade * s), (uint8_t)(b * fade * s));
    }
    led_strip_refresh(strip);
    pos = (pos + 1) % LED_COUNT;
}

/* Preset 13: random sparkles on top of dim base (uses r,g,b as accent). */
static void anim_sparkle(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    if (!strip) return;
    float s = bright / 255.0f;
    for (int i = 0; i < LED_COUNT; i++) {
        led_strip_set_pixel(strip, i,
            (uint8_t)(r * s * 0.08f), (uint8_t)(g * s * 0.08f), (uint8_t)(b * s * 0.08f));
    }
    for (int k = 0; k < 4; k++) {
        uint32_t rnd = esp_random();
        int i = (int)(rnd % (uint32_t)LED_COUNT);
        float f = 0.35f + 0.65f * ((float)((rnd >> 8) & 0xff) / 255.0f);
        led_strip_set_pixel(strip, i,
            (uint8_t)(r * f * s), (uint8_t)(g * f * s), (uint8_t)(b * f * s));
    }
    led_strip_refresh(strip);
}

/* Preset 14: bright head + short tail moving around the ring (comet). */
static void anim_comet(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    static int pos = 0;
    if (!strip) return;
    float s = bright / 255.0f;
    led_strip_clear(strip);
    for (int k = 0; k < 6; k++) {
        int i = (pos - k + LED_COUNT * 2) % LED_COUNT;
        float fade = (6.0f - (float)k) / 6.0f;
        if (fade < 0.0f) {
            fade = 0.0f;
        }
        led_strip_set_pixel(strip, i,
            (uint8_t)(r * fade * s), (uint8_t)(g * fade * s), (uint8_t)(b * fade * s));
    }
    led_strip_refresh(strip);
    pos = (pos + 1) % LED_COUNT;
}

/* Preset 6: per-LED colors (WS2812 strip has LED_COUNT independent pixels). */
static void anim_pixels(const uint8_t px[LED_COUNT][3], uint8_t bright) {
    if (!strip) return;
    float s = bright / 255.0f;
    for (int i = 0; i < LED_COUNT; i++) {
        led_strip_set_pixel(strip, i,
            (uint8_t)(px[i][0] * s), (uint8_t)(px[i][1] * s), (uint8_t)(px[i][2] * s));
    }
    led_strip_refresh(strip);
}

static void anim_pixels_pulse(const uint8_t px[LED_COUNT][3], uint8_t bright) {
    static int level = 0;
    static int dir = 1;
    if (!strip) return;
    float pulse = 0.15f + 0.85f * ((float)level / 100.0f);
    float s = (bright / 255.0f) * pulse;
    for (int i = 0; i < LED_COUNT; i++) {
        led_strip_set_pixel(strip, i,
            (uint8_t)(px[i][0] * s), (uint8_t)(px[i][1] * s), (uint8_t)(px[i][2] * s));
    }
    led_strip_refresh(strip);
    level += dir * 3;
    if (level >= 100) {
        level = 100;
        dir = -1;
    } else if (level <= 0) {
        level = 0;
        dir = 1;
    }
}

static bool pixels_all_uniform(const uint8_t px[LED_COUNT][3]) {
    uint8_t r0 = px[0][0], g0 = px[0][1], b0 = px[0][2];
    for (int i = 1; i < LED_COUNT; i++) {
        if (px[i][0] != r0 || px[i][1] != g0 || px[i][2] != b0) {
            return false;
        }
    }
    return true;
}

static void anim_pixels_fade(const uint8_t px[LED_COUNT][3], uint8_t bright) {
    static uint8_t mix = 0;
    if (!strip) return;
    float t = mix / 255.0f;
    float s = bright / 255.0f;
    for (int i = 0; i < LED_COUNT; i++) {
        int j = (i + 1) % LED_COUNT;
        float rf = px[i][0] * (1.0f - t) + px[j][0] * t;
        float gf = px[i][1] * (1.0f - t) + px[j][1] * t;
        float bf = px[i][2] * (1.0f - t) + px[j][2] * t;
        led_strip_set_pixel(strip, i, (uint8_t)(rf * s), (uint8_t)(gf * s), (uint8_t)(bf * s));
    }
    led_strip_refresh(strip);
    mix = (uint8_t)(mix + 6);
}

static void led_task(void *arg) {
    const led_strip_config_t led_config = {
        .strip_gpio_num = LED_GPIO,
        .max_leds = LED_COUNT,
        .led_pixel_format = LED_PIXEL_FORMAT_GRB,
        .led_model = LED_MODEL_WS2812,
    };
    const led_strip_rmt_config_t rmt_config = {};
    esp_err_t ret = led_strip_new_rmt_device(&led_config, &rmt_config, &strip);
    if (ret != ESP_OK || !strip) {
        ESP_LOGE(TAG, "WS2812 init failed: %d", ret);
        vTaskDelete(NULL);
        return;
    }
    anim_clear();
    ESP_LOGI(TAG, "WS2812 initialized");
    vTaskDelay(pdMS_TO_TICKS(100));

    while (1) {
        uint32_t now_ms = esp_log_timestamp();
        portENTER_CRITICAL(&led_state_mux);
        if (led_cue_until_ms != 0 && (int32_t)(now_ms - led_cue_until_ms) >= 0) {
            led_cue_until_ms = 0;
            led_state.on = false;
            led_state.preset = 0;
        }
        portEXIT_CRITICAL(&led_state_mux);

        struct { bool on; int preset; uint8_t r,g,b; uint8_t brightness; int auto_off_ms; uint8_t px[LED_COUNT][3]; } s;
        portENTER_CRITICAL(&led_state_mux);
        s.on = led_state.on;
        s.preset = led_state.preset;
        s.r = led_state.r;
        s.g = led_state.g;
        s.b = led_state.b;
        s.brightness = led_state.brightness;
        s.auto_off_ms = led_state.auto_off_ms;
        memcpy(s.px, (const void *)led_state.pix, sizeof(s.px));
        portEXIT_CRITICAL(&led_state_mux);

        if (s.brightness > LED_GLOBAL_BRIGHTNESS_CAP) {
            s.brightness = LED_GLOBAL_BRIGHTNESS_CAP;
        }

        if (!s.on || s.preset == 0) {
            anim_clear();
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        switch (s.preset) {
            case 1: anim_solid(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(50)); break;
            case 2: anim_spinner(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(60)); break;
            case 3: anim_rainbow(s.brightness); vTaskDelay(pdMS_TO_TICKS(35)); break;
            case 4: anim_breathe(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(25)); break;
            case 5: anim_chase(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(45)); break;
            case 6: anim_pixels(s.px, s.brightness); vTaskDelay(pdMS_TO_TICKS(50)); break;
            case 10: anim_pixels_pulse(s.px, s.brightness); vTaskDelay(pdMS_TO_TICKS(35)); break;
            case 11:
                if (pixels_all_uniform(s.px)) {
                    anim_breathe(s.px[0][0], s.px[0][1], s.px[0][2], s.brightness);
                } else {
                    anim_pixels_fade(s.px, s.brightness);
                }
                vTaskDelay(pdMS_TO_TICKS(35));
                break;
            case 12: anim_rainbow_reverse(s.brightness); vTaskDelay(pdMS_TO_TICKS(35)); break;
            case 13: anim_sparkle(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(40)); break;
            case 14: anim_comet(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(40)); break;
            case 15: anim_spinner_reverse(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(60)); break;
            default: anim_clear(); vTaskDelay(pdMS_TO_TICKS(50)); break;
        }
    }
}

static void led_start_breath_cue(uint8_t r, uint8_t g, uint8_t b, uint32_t duration_ms) {
    portENTER_CRITICAL(&led_state_mux);
    led_state.on = true;
    led_state.preset = 4;
    led_state.r = r;
    led_state.g = g;
    led_state.b = b;
    led_state.brightness = 220;
    led_cue_until_ms = esp_log_timestamp() + duration_ms;
    portEXIT_CRITICAL(&led_state_mux);
}

static volatile bool wifi_connected = false;
#define WIFI_PROFILE_MAX 5
typedef struct {
    char ssid[33];
    char password[65];
    bool used;
} wifi_profile_t;
static wifi_profile_t wifi_profiles[WIFI_PROFILE_MAX];
static int wifi_profile_count = 0;
static int wifi_active_index = 0;
static int wifi_connected_index = -1;
static uint8_t wifi_retry_same = 0;
static portMUX_TYPE wifi_mux = portMUX_INITIALIZER_UNLOCKED;

static void wifi_profiles_set_single_fallback(void) {
    memset(wifi_profiles, 0, sizeof(wifi_profiles));
    wifi_profile_count = 0;
    wifi_active_index = 0;
    if (strlen(KORVO_WIFI_SSID) > 0) {
        wifi_profiles[0].used = true;
        strncpy(wifi_profiles[0].ssid, KORVO_WIFI_SSID, sizeof(wifi_profiles[0].ssid) - 1);
        strncpy(wifi_profiles[0].password, KORVO_WIFI_PASSWORD, sizeof(wifi_profiles[0].password) - 1);
        wifi_profile_count = 1;
    }
}

static void wifi_profiles_load_from_nvs(void) {
    nvs_handle_t nvs = 0;
    bool loaded = false;
    if (nvs_open("wifi_cfg", NVS_READONLY, &nvs) == ESP_OK) {
        uint8_t count_u8 = 0;
        uint8_t active_u8 = 0;
        if (nvs_get_u8(nvs, "count", &count_u8) == ESP_OK && count_u8 > 0) {
            memset(wifi_profiles, 0, sizeof(wifi_profiles));
            wifi_profile_count = 0;
            for (int i = 0; i < WIFI_PROFILE_MAX && i < (int)count_u8; i++) {
                char key_ssid[8];
                char key_pwd[8];
                snprintf(key_ssid, sizeof(key_ssid), "s%d", i);
                snprintf(key_pwd, sizeof(key_pwd), "p%d", i);
                size_t ssid_len = sizeof(wifi_profiles[i].ssid);
                size_t pwd_len = sizeof(wifi_profiles[i].password);
                if (nvs_get_str(nvs, key_ssid, wifi_profiles[i].ssid, &ssid_len) == ESP_OK &&
                    strlen(wifi_profiles[i].ssid) > 0) {
                    if (nvs_get_str(nvs, key_pwd, wifi_profiles[i].password, &pwd_len) != ESP_OK) {
                        wifi_profiles[i].password[0] = '\0';
                    }
                    wifi_profiles[i].used = true;
                    wifi_profile_count++;
                }
            }
            if (nvs_get_u8(nvs, "active", &active_u8) == ESP_OK && wifi_profile_count > 0) {
                wifi_active_index = (int)active_u8;
                if (wifi_active_index < 0 || wifi_active_index >= wifi_profile_count) {
                    wifi_active_index = 0;
                }
            } else {
                wifi_active_index = 0;
            }
            loaded = wifi_profile_count > 0;
        }
        nvs_close(nvs);
    }
    if (!loaded) {
        wifi_profiles_set_single_fallback();
    }
}

static void wifi_profiles_save_to_nvs(void) {
    nvs_handle_t nvs = 0;
    if (nvs_open("wifi_cfg", NVS_READWRITE, &nvs) != ESP_OK) {
        return;
    }
    uint8_t count_u8 = (uint8_t)((wifi_profile_count < 0) ? 0 : ((wifi_profile_count > WIFI_PROFILE_MAX) ? WIFI_PROFILE_MAX : wifi_profile_count));
    uint8_t active_u8 = (uint8_t)((wifi_active_index < 0) ? 0 : wifi_active_index);
    nvs_set_u8(nvs, "count", count_u8);
    nvs_set_u8(nvs, "active", active_u8);
    for (int i = 0; i < WIFI_PROFILE_MAX; i++) {
        char key_ssid[8];
        char key_pwd[8];
        snprintf(key_ssid, sizeof(key_ssid), "s%d", i);
        snprintf(key_pwd, sizeof(key_pwd), "p%d", i);
        if (i < wifi_profile_count && wifi_profiles[i].used && wifi_profiles[i].ssid[0] != '\0') {
            nvs_set_str(nvs, key_ssid, wifi_profiles[i].ssid);
            nvs_set_str(nvs, key_pwd, wifi_profiles[i].password);
        } else {
            nvs_erase_key(nvs, key_ssid);
            nvs_erase_key(nvs, key_pwd);
        }
    }
    nvs_commit(nvs);
    nvs_close(nvs);
}

static esp_err_t wifi_apply_profile_index(int idx) {
    if (idx < 0 || idx >= wifi_profile_count || !wifi_profiles[idx].used) {
        return ESP_ERR_INVALID_ARG;
    }
    wifi_config_t wifi_config = {0};
    strncpy((char *)wifi_config.sta.ssid, wifi_profiles[idx].ssid, sizeof(wifi_config.sta.ssid) - 1);
    strncpy((char *)wifi_config.sta.password, wifi_profiles[idx].password, sizeof(wifi_config.sta.password) - 1);
    esp_err_t err = esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
    if (err == ESP_OK) {
        wifi_active_index = idx;
    }
    return err;
}

/* Scan visible APs and pick first matching saved profile (priority starts from current active profile). */
static int wifi_find_visible_profile_index(void) {
    if (wifi_profile_count <= 0) {
        return -1;
    }
    wifi_scan_config_t scan_cfg = {0};
    esp_err_t se = esp_wifi_scan_start(&scan_cfg, true);
    if (se != ESP_OK) {
        ESP_LOGW(TAG, "WiFi scan failed: %s", esp_err_to_name(se));
        return -1;
    }
    uint16_t ap_num = 0;
    if (esp_wifi_scan_get_ap_num(&ap_num) != ESP_OK || ap_num == 0) {
        return -1;
    }
    wifi_ap_record_t *aps = (wifi_ap_record_t *)calloc(ap_num, sizeof(wifi_ap_record_t));
    if (!aps) {
        return -1;
    }
    uint16_t got = ap_num;
    int selected = -1;
    if (esp_wifi_scan_get_ap_records(&got, aps) == ESP_OK) {
        for (int off = 0; off < wifi_profile_count; off++) {
            int idx = (wifi_active_index + off) % wifi_profile_count;
            if (!wifi_profiles[idx].used || wifi_profiles[idx].ssid[0] == '\0') {
                continue;
            }
            for (uint16_t i = 0; i < got; i++) {
                if (strcmp((const char *)aps[i].ssid, wifi_profiles[idx].ssid) == 0) {
                    selected = idx;
                    goto done;
                }
            }
        }
    }
done:
    free(aps);
    return selected;
}

static void korvo_init_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_connected = false;
        wifi_event_sta_disconnected_t *disconn = (wifi_event_sta_disconnected_t *)event_data;
        ESP_LOGW(TAG, "WiFi disconnected (reason=%d)", disconn ? disconn->reason : -1);
        if (wifi_profile_count <= 0) {
            esp_wifi_connect();
            return;
        }
        if (wifi_retry_same < 1) {
            wifi_retry_same++;
            esp_wifi_connect();
            return;
        }
        wifi_retry_same = 0;
        int chosen = wifi_find_visible_profile_index();
        if (chosen < 0) {
            chosen = (wifi_active_index + 1) % wifi_profile_count;
        }
        if (wifi_apply_profile_index(chosen) == ESP_OK) {
            ESP_LOGI(TAG, "Trying WiFi profile %d/%d: %s", chosen + 1, wifi_profile_count, wifi_profiles[chosen].ssid);
        }
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ESP_LOGI(TAG, "WiFi connected!");
        
        // Initialize mDNS
        if (mdns_init() == ESP_OK) {
            mdns_hostname_set("korvo");
            mdns_instance_name_set("Korvo Wake Word Agent");
            mdns_service_add(NULL, "_http", "_tcp", 80, NULL, 0);
            ESP_LOGI(TAG, "mDNS initialized - accessible at http://korvo.local");
        }

        wifi_connected = true;
        wifi_retry_same = 0;
        wifi_connected_index = wifi_active_index;
        wifi_profiles_save_to_nvs();
        wifi_ap_record_t rec = {0};
        if (esp_wifi_sta_get_ap_info(&rec) == ESP_OK) {
            ESP_LOGI(TAG, "Connected SSID: %s", (const char *)rec.ssid);
        }
        /* Momentary green breath — not a continuous "WiFi OK" animation. */
        led_start_breath_cue(0, 210, 100, LED_CUE_BREATH_MS);
    }
}

static void wifi_init_sta(void)
{
    wifi_profiles_load_from_nvs();
    if (wifi_profile_count > 0) {
        ESP_LOGI(TAG, "Connecting to profile %d/%d: %s", wifi_active_index + 1, wifi_profile_count, wifi_profiles[wifi_active_index].ssid);
    } else {
        ESP_LOGW(TAG, "No WiFi profiles configured yet");
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    /* Create AP netif up front so we can fall back to APSTA without re-init WiFi. */
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_event_handler_instance_t inst_any, inst_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &inst_any));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &inst_ip));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    if (wifi_profile_count > 0) {
        ESP_ERROR_CHECK(wifi_apply_profile_index(wifi_active_index));
    }
    ESP_ERROR_CHECK(esp_wifi_start());
}

/* When STA cannot join the AP, keep running: expose SoftAP + HTTP on 192.168.4.1 instead of reboot-looping. */
static void wifi_start_softap_fallback(void)
{
    wifi_config_t ap_config = {0};
    const char *ap_ssid = "Korvo-Setup";
    strncpy((char *)ap_config.ap.ssid, ap_ssid, sizeof(ap_config.ap.ssid) - 1);
    ap_config.ap.ssid_len = (uint8_t)strlen(ap_ssid);
    ap_config.ap.channel = 1;
    ap_config.ap.max_connection = 4;
    ap_config.ap.authmode = WIFI_AUTH_OPEN;

    wifi_config_t sta_config = {0};
    if (wifi_profile_count > 0 && wifi_active_index >= 0 && wifi_active_index < wifi_profile_count) {
        strncpy((char *)sta_config.sta.ssid, wifi_profiles[wifi_active_index].ssid, sizeof(sta_config.sta.ssid) - 1);
        strncpy((char *)sta_config.sta.password, wifi_profiles[wifi_active_index].password, sizeof(sta_config.sta.password) - 1);
    }

    esp_err_t err = esp_wifi_stop();
    if (err != ESP_OK && err != ESP_ERR_WIFI_NOT_INIT) {
        ESP_LOGW(TAG, "wifi_stop before SoftAP: %s", esp_err_to_name(err));
    }
    if (esp_wifi_set_mode(WIFI_MODE_APSTA) != ESP_OK) {
        ESP_LOGE(TAG, "SoftAP fallback: set_mode APSTA failed");
        return;
    }
    if (esp_wifi_set_config(WIFI_IF_STA, &sta_config) != ESP_OK) {
        ESP_LOGE(TAG, "SoftAP fallback: set_config STA failed");
    }
    if (esp_wifi_set_config(WIFI_IF_AP, &ap_config) != ESP_OK) {
        ESP_LOGE(TAG, "SoftAP fallback: set_config AP failed");
        return;
    }
    if (esp_wifi_start() != ESP_OK) {
        ESP_LOGE(TAG, "SoftAP fallback: wifi_start failed");
        return;
    }
    ESP_LOGW(TAG, "WiFi STA failed — SoftAP up: SSID=%s open, browse http://192.168.4.1 (STA keeps retrying in background)", ap_ssid);
}

static esp_err_t audio_stream_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "audio/wav");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Cache-Control", "no-cache, no-store");
    httpd_resp_set_status(req, "200 OK");
    ESP_LOGI(TAG, "HTTP audio client connected (stream)");
    stream_client_connected = true;
    /* 16 kHz mono s16le — must match esp_board_init(..., 1 channel) and feed chunks */
    unsigned char wav_header[] = {
        'R','I','F','F', 0xFF,0xFF,0xFF,0xFF,
        'W','A','V','E',
        'f','m','t',' ', 0x10,0x00,0x00,0x00, 0x01,0x00, 0x01,0x00,
        0x80,0x3E,0x00,0x00, 0x00,0x7D,0x00,0x00, 0x02,0x00, 0x10,0x00,
        'd','a','t','a', 0xFE,0xFF,0xFF,0xFF
    };
    httpd_resp_send_chunk(req, (const char*)wav_header, sizeof(wav_header));
    unsigned char *buffer = malloc(AUDIO_CHUNK_BYTES);
    if (!buffer) { stream_client_connected = false; httpd_resp_send_500(req); return ESP_ERR_NO_MEM; }
    int chunks = 0;
    size_t pcm_out = 0;
    uint32_t log_next_ms = esp_log_timestamp() + 2000;
    while (stream_client_connected) {
        size_t item_size = 0;
        /* BYTEBUF merge: xRingbufferReceive can return the whole backlog; ReceiveUpTo caps one PCM chunk. */
        void *item = xRingbufferReceiveUpTo(audio_ringbuf, &item_size, pdMS_TO_TICKS(200), AUDIO_CHUNK_BYTES);
        if (item && item_size > 0) {
            memcpy(buffer, item, item_size);
            vRingbufferReturnItem(audio_ringbuf, item);
            esp_err_t send_err = httpd_resp_send_chunk(req, (const char *)buffer, item_size);
            if (send_err != ESP_OK) {
                /* Short backpressure window: avoid tearing down the stream on transient EAGAIN. */
                bool recovered = false;
                for (int retry = 0; retry < 6; retry++) {
                    if (errno != EAGAIN && errno != 11) {
                        break;
                    }
                    vTaskDelay(TICKS_AT_LEAST_1(4));
                    send_err = httpd_resp_send_chunk(req, (const char *)buffer, item_size);
                    if (send_err == ESP_OK) {
                        recovered = true;
                        break;
                    }
                }
                if (!recovered && send_err != ESP_OK) {
                    break;
                }
            }
            chunks++;
            pcm_out += item_size;
            if (esp_log_timestamp() >= log_next_ms) {
                ESP_LOGI(TAG, "audio stream: sent %d chunks, %u KB PCM (client still connected)", chunks, (unsigned)(pcm_out / 1024u));
                log_next_ms = esp_log_timestamp() + 2000;
            }
        } else {
            continue;
        }
    }
    if (buffer) free(buffer);
    stream_client_connected = false;
    ESP_LOGI(TAG, "HTTP audio client disconnected (sent %d chunks, %u KB PCM)", chunks, (unsigned)(pcm_out / 1024u));
    httpd_resp_send_chunk(req, NULL, 0);
    return ESP_OK;
}

#define LED_POST_MAX 1600
/* GET/POST /api/led JSON: header + 12 * "[255,255,255]," — keep headroom if LED_COUNT grows. */
#define LED_STATE_JSON_BUF 2048

/* Browsers treat 500/HTML without ACAO as a CORS failure → fetch() "network error" even if LEDs updated. */
static esp_err_t led_send_json_error_cors(httpd_req_t *req, const char *json_body) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_status(req, "500 Internal Server Error");
    return httpd_resp_send(req, json_body, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t led_send_json_status_cors(httpd_req_t *req, const char *status, const char *json_body) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_status(req, status);
    return httpd_resp_send(req, json_body, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t led_send_state_json(httpd_req_t *req) {
    bool on;
    int preset;
    uint8_t r, g, b, br;
    uint8_t px[LED_COUNT][3];
    portENTER_CRITICAL(&led_state_mux);
    on = led_state.on;
    preset = led_state.preset;
    r = led_state.r;
    g = led_state.g;
    b = led_state.b;
    br = led_state.brightness;
    memcpy(px, (const void *)led_state.pix, sizeof(px));
    portEXIT_CRITICAL(&led_state_mux);
    /* led_state.pix is only maintained for preset 6; solid / animations use r,g,b on hardware. */
    if (on && preset == 1) {
        for (int i = 0; i < LED_COUNT; i++) {
            px[i][0] = r;
            px[i][1] = g;
            px[i][2] = b;
        }
    } else if (!on || preset == 0) {
        memset(px, 0, sizeof(px));
    } else if (on && preset >= 2 && preset <= 15 && preset != 6) {
        /* Spinner / rainbow / chase / pulse / fade / … — mirror r,g,b for JSON clients (not per-pixel mode). */
        for (int i = 0; i < LED_COUNT; i++) {
            px[i][0] = r;
            px[i][1] = g;
            px[i][2] = b;
        }
    }
    char out[LED_STATE_JSON_BUF];
    const size_t out_cap = sizeof(out);
    int n = snprintf(out, out_cap,
        "{\"on\":%s,\"preset\":%d,\"r\":%u,\"g\":%u,\"b\":%u,\"brightness\":%u,\"led_count\":%d,\"pixels\":[",
        on ? "true" : "false", preset, (unsigned)r, (unsigned)g, (unsigned)b, (unsigned)br, LED_COUNT);
    if (n <= 0 || (size_t)n >= out_cap) {
        ESP_LOGE(TAG, "led_send_state_json: header snprintf failed or truncated (n=%d cap=%u)", n, (unsigned)out_cap);
        return led_send_json_error_cors(req, "{\"error\":\"led_json_header\"}");
    }
    for (int i = 0; i < LED_COUNT; i++) {
        int add = snprintf(out + n, out_cap - (size_t)n, "%s[%u,%u,%u]",
            (i > 0) ? "," : "", (unsigned)px[i][0], (unsigned)px[i][1], (unsigned)px[i][2]);
        if (add <= 0 || (size_t)(n + add) >= out_cap) {
            ESP_LOGE(TAG, "led_send_state_json: pixel %d truncated (n=%d add=%d cap=%u)", i, n, add, (unsigned)out_cap);
            return led_send_json_error_cors(req, "{\"error\":\"led_json_overflow\"}");
        }
        n += add;
    }
    {
        int tail = snprintf(out + n, out_cap - (size_t)n, "]}");
        if (tail <= 0 || (size_t)(n + tail) >= out_cap) {
            ESP_LOGE(TAG, "led_send_state_json: tail truncated");
            return led_send_json_error_cors(req, "{\"error\":\"led_json_overflow\"}");
        }
        n += tail;
    }
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, out, (size_t)n);
}

static esp_err_t led_api_get_handler(httpd_req_t *req) {
    return led_send_state_json(req);
}

static esp_err_t led_api_options_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
    httpd_resp_set_status(req, "204 No Content");
    return httpd_resp_send(req, NULL, 0);
}

/* STA IPv4 for dashboards that want to avoid slow mDNS (.local) on every request. */
static esp_err_t network_status_get_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        return httpd_resp_send_500(req);
    }
    esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    char ip_buf[24] = "";
    char ssid_buf[33] = "";
    bool sta_connected = false;
    esp_netif_ip_info_t ip_info;
    if (sta && esp_netif_get_ip_info(sta, &ip_info) == ESP_OK && ip_info.ip.addr != 0) {
        snprintf(ip_buf, sizeof(ip_buf), IPSTR, IP2STR(&ip_info.ip));
    }
    wifi_ap_record_t ap = {0};
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        sta_connected = true;
        strncpy(ssid_buf, (const char *)ap.ssid, sizeof(ssid_buf) - 1);
    }
    cJSON_AddStringToObject(root, "sta_ip", ip_buf);
    cJSON_AddBoolToObject(root, "sta_connected", sta_connected);
    cJSON_AddStringToObject(root, "sta_ssid", ssid_buf);
    cJSON_AddNumberToObject(root, "active_index", wifi_active_index);
    cJSON_AddNumberToObject(root, "connected_index", wifi_connected_index);
    cJSON_AddNumberToObject(root, "profile_count", wifi_profile_count);
    cJSON *arr = cJSON_AddArrayToObject(root, "known_networks");
    for (int i = 0; i < wifi_profile_count && i < WIFI_PROFILE_MAX; i++) {
        if (!wifi_profiles[i].used || wifi_profiles[i].ssid[0] == '\0') {
            continue;
        }
        cJSON *item = cJSON_CreateObject();
        if (!item) {
            continue;
        }
        cJSON_AddNumberToObject(item, "index", i);
        cJSON_AddStringToObject(item, "ssid", wifi_profiles[i].ssid);
        cJSON_AddBoolToObject(item, "is_active", i == wifi_active_index);
        cJSON_AddItemToArray(arr, item);
    }
    char *out = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!out) {
        return httpd_resp_send_500(req);
    }
    esp_err_t res = httpd_resp_send(req, out, HTTPD_RESP_USE_STRLEN);
    free(out);
    return res;
}

static esp_err_t network_status_options_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
    httpd_resp_set_status(req, "204 No Content");
    return httpd_resp_send(req, NULL, 0);
}

/* Read POST body; supports Content-Length missing or zero (read until drain). */
static int led_http_read_body(httpd_req_t *req, char *out, size_t cap) {
    int declared = req->content_len;
    if (declared > (int)(cap - 1)) {
        return -2;
    }
    if (declared > 0) {
        int got = 0;
        while (got < declared) {
            int r = httpd_req_recv(req, out + got, (size_t)(declared - got));
            if (r <= 0) {
                return -1;
            }
            got += r;
        }
        out[got] = '\0';
        return got;
    }
    int got = 0;
    for (;;) {
        int space = (int)cap - 1 - got;
        if (space <= 0) {
            break;
        }
        int r = httpd_req_recv(req, out + got, (size_t)space);
        if (r < 0) {
            return -1;
        }
        if (r == 0) {
            break;
        }
        got += r;
        /* Some clients don't provide Content-Length. In that mode, reading until r==0
           can stall for the socket timeout even after full JSON body arrived.
           If we got fewer bytes than requested, treat it as end-of-body. */
        if (r < space) {
            break;
        }
    }
    out[got] = '\0';
    return got;
}

static esp_err_t network_profiles_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    char body[1400];
    int got = led_http_read_body(req, body, sizeof(body));
    if (got <= 0) {
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_body\"}", HTTPD_RESP_USE_STRLEN);
    }
    cJSON *root = cJSON_Parse(body);
    if (!root) {
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_json\"}", HTTPD_RESP_USE_STRLEN);
    }
    cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "networks");
    if (!cJSON_IsArray(arr)) {
        cJSON_Delete(root);
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"networks_array_required\"}", HTTPD_RESP_USE_STRLEN);
    }
    wifi_profile_t incoming[WIFI_PROFILE_MAX];
    memset(incoming, 0, sizeof(incoming));
    int incoming_count = 0;
    int active_idx = 0;
    int arr_sz = cJSON_GetArraySize(arr);
    for (int i = 0; i < arr_sz && incoming_count < WIFI_PROFILE_MAX; i++) {
        cJSON *item = cJSON_GetArrayItem(arr, i);
        if (!cJSON_IsObject(item)) {
            continue;
        }
        cJSON *jssid = cJSON_GetObjectItemCaseSensitive(item, "ssid");
        cJSON *jpwd = cJSON_GetObjectItemCaseSensitive(item, "password");
        cJSON *jactive = cJSON_GetObjectItemCaseSensitive(item, "is_active");
        const char *ssid = cJSON_IsString(jssid) ? jssid->valuestring : "";
        const char *pwd = cJSON_IsString(jpwd) ? jpwd->valuestring : "";
        if (!ssid || strlen(ssid) == 0) {
            continue;
        }
        incoming[incoming_count].used = true;
        strncpy(incoming[incoming_count].ssid, ssid, sizeof(incoming[incoming_count].ssid) - 1);
        strncpy(incoming[incoming_count].password, pwd ? pwd : "", sizeof(incoming[incoming_count].password) - 1);
        if (cJSON_IsTrue(jactive)) {
            active_idx = incoming_count;
        }
        incoming_count++;
    }
    cJSON *jactive_idx = cJSON_GetObjectItemCaseSensitive(root, "active_index");
    if (cJSON_IsNumber(jactive_idx)) {
        int req_idx = (int)cJSON_GetNumberValue(jactive_idx);
        if (req_idx >= 0 && req_idx < incoming_count) {
            active_idx = req_idx;
        }
    }
    cJSON_Delete(root);
    if (incoming_count <= 0) {
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"no_valid_networks\"}", HTTPD_RESP_USE_STRLEN);
    }

    portENTER_CRITICAL(&wifi_mux);
    memset(wifi_profiles, 0, sizeof(wifi_profiles));
    for (int i = 0; i < incoming_count; i++) {
        wifi_profiles[i] = incoming[i];
    }
    wifi_profile_count = incoming_count;
    wifi_active_index = active_idx;
    wifi_retry_same = 0;
    portEXIT_CRITICAL(&wifi_mux);
    wifi_profiles_save_to_nvs();

    if (wifi_apply_profile_index(active_idx) == ESP_OK) {
        esp_wifi_connect();
    }

    char out[160];
    snprintf(out, sizeof(out), "{\"ok\":true,\"profile_count\":%d,\"active_index\":%d}", incoming_count, active_idx);
    return httpd_resp_send(req, out, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t led_api_post_handler(httpd_req_t *req) {
    int declared = req->content_len;
    if (declared > LED_POST_MAX) {
        return led_send_json_status_cors(req, "400 Bad Request", "{\"error\":\"bad content length\"}");
    }
    char *body = (char *)malloc(LED_POST_MAX + 1);
    if (!body) {
        return led_send_json_status_cors(req, "500 Internal Server Error", "{\"error\":\"oom\"}");
    }
    memset(body, 0, LED_POST_MAX + 1);
    int total = led_http_read_body(req, body, LED_POST_MAX + 1);
    if (total < 0) {
        free(body);
        if (total == -2) {
            return led_send_json_status_cors(req, "400 Bad Request", "{\"error\":\"body too large\"}");
        }
        return led_send_json_status_cors(req, "400 Bad Request", "{\"error\":\"recv\"}");
    }
    if (total == 0) {
        free(body);
        return led_send_json_status_cors(req, "400 Bad Request", "{\"error\":\"empty body\"}");
    }
    cJSON *root = cJSON_Parse(body);
    free(body);
    if (!root) {
        return led_send_json_status_cors(req, "400 Bad Request", "{\"error\":\"invalid json\"}");
    }

    bool json_on_present = false;
    bool json_on_value = false;
    const cJSON *j_on = cJSON_GetObjectItemCaseSensitive(root, "on");
    if (cJSON_IsBool(j_on)) {
        json_on_present = true;
        json_on_value = cJSON_IsTrue(j_on);
    }

    bool json_pixels = false;
    uint8_t pxcopy[LED_COUNT][3];
    const cJSON *jpx = cJSON_GetObjectItemCaseSensitive(root, "pixels");
    if (cJSON_IsArray(jpx) && cJSON_GetArraySize(jpx) == LED_COUNT) {
        json_pixels = true;
        for (int i = 0; i < LED_COUNT; i++) {
            pxcopy[i][0] = 0;
            pxcopy[i][1] = 0;
            pxcopy[i][2] = 0;
            const cJSON *cell = cJSON_GetArrayItem(jpx, i);
            if (cJSON_IsArray(cell) && cJSON_GetArraySize(cell) >= 3) {
                for (int k = 0; k < 3; k++) {
                    const cJSON *cv = cJSON_GetArrayItem(cell, k);
                    double v = cJSON_IsNumber(cv) ? cJSON_GetNumberValue(cv) : 0.0;
                    if (v < 0) {
                        v = 0;
                    }
                    if (v > 255) {
                        v = 255;
                    }
                    pxcopy[i][k] = (uint8_t)v;
                }
            }
        }
    }

    bool json_one = false;
    int one_i = 0;
    uint8_t one_r = 0, one_g = 0, one_b = 0;
    const cJSON *jone = cJSON_GetObjectItemCaseSensitive(root, "pixel");
    if (cJSON_IsObject(jone)) {
        const cJSON *ji = cJSON_GetObjectItemCaseSensitive(jone, "i");
        if (cJSON_IsNumber(ji)) {
            int ix = (int)cJSON_GetNumberValue(ji);
            if (ix >= 0 && ix < LED_COUNT) {
                json_one = true;
                one_i = ix;
                const cJSON *jr = cJSON_GetObjectItemCaseSensitive(jone, "r");
                const cJSON *jg = cJSON_GetObjectItemCaseSensitive(jone, "g");
                const cJSON *jb = cJSON_GetObjectItemCaseSensitive(jone, "b");
                double vr = cJSON_IsNumber(jr) ? cJSON_GetNumberValue(jr) : 0.0;
                double vg = cJSON_IsNumber(jg) ? cJSON_GetNumberValue(jg) : 0.0;
                double vb = cJSON_IsNumber(jb) ? cJSON_GetNumberValue(jb) : 0.0;
                if (vr < 0) {
                    vr = 0;
                }
                if (vr > 255) {
                    vr = 255;
                }
                if (vg < 0) {
                    vg = 0;
                }
                if (vg > 255) {
                    vg = 255;
                }
                if (vb < 0) {
                    vb = 0;
                }
                if (vb > 255) {
                    vb = 255;
                }
                one_r = (uint8_t)vr;
                one_g = (uint8_t)vg;
                one_b = (uint8_t)vb;
            }
        }
    }

    bool json_preset = false;
    int json_preset_val = 0;
    const cJSON *jpre = cJSON_GetObjectItemCaseSensitive(root, "preset");
    if (cJSON_IsNumber(jpre)) {
        json_preset = true;
        json_preset_val = (int)cJSON_GetNumberValue(jpre);
        if (json_preset_val < 0) {
            json_preset_val = 0;
        }
        if (json_preset_val > 20) {
            json_preset_val = 20;
        }
    }
    bool json_r = false, json_g = false, json_b = false, json_br = false;
    uint8_t v_r = 0, v_g = 0, v_b = 0, v_br = 0;
    const cJSON *jr = cJSON_GetObjectItemCaseSensitive(root, "r");
    if (cJSON_IsNumber(jr)) {
        double v = cJSON_GetNumberValue(jr);
        if (v < 0) {
            v = 0;
        }
        if (v > 255) {
            v = 255;
        }
        v_r = (uint8_t)v;
        json_r = true;
    }
    const cJSON *jg = cJSON_GetObjectItemCaseSensitive(root, "g");
    if (cJSON_IsNumber(jg)) {
        double v = cJSON_GetNumberValue(jg);
        if (v < 0) {
            v = 0;
        }
        if (v > 255) {
            v = 255;
        }
        v_g = (uint8_t)v;
        json_g = true;
    }
    const cJSON *jb = cJSON_GetObjectItemCaseSensitive(root, "b");
    if (cJSON_IsNumber(jb)) {
        double v = cJSON_GetNumberValue(jb);
        if (v < 0) {
            v = 0;
        }
        if (v > 255) {
            v = 255;
        }
        v_b = (uint8_t)v;
        json_b = true;
    }
    const cJSON *jbr = cJSON_GetObjectItemCaseSensitive(root, "brightness");
    if (cJSON_IsNumber(jbr)) {
        double v = cJSON_GetNumberValue(jbr);
        if (v < 0) {
            v = 0;
        }
        if (v > 255) {
            v = 255;
        }
        v_br = (uint8_t)v;
        json_br = true;
    }

    portENTER_CRITICAL(&led_state_mux);
    {
        led_cue_until_ms = 0;
        if (json_preset) {
            led_state.preset = json_preset_val;
        }
        if (json_r) {
            led_state.r = v_r;
        }
        if (json_g) {
            led_state.g = v_g;
        }
        if (json_b) {
            led_state.b = v_b;
        }
        if (json_br) {
            led_state.brightness = v_br;
        }
        if (json_one) {
            led_state.pix[one_i][0] = one_r;
            led_state.pix[one_i][1] = one_g;
            led_state.pix[one_i][2] = one_b;
            led_state.preset = 6;
            if (!json_on_present) {
                led_state.on = true;
            }
        }
        if (json_pixels) {
            memcpy((void *)led_state.pix, pxcopy, sizeof(pxcopy));
            if (json_preset && (json_preset_val == 10 || json_preset_val == 11)) {
                led_state.preset = json_preset_val;
            } else {
                led_state.preset = 6;
            }
            if (!json_on_present) {
                led_state.on = true;
            }
        }
        if (json_on_present) {
            led_state.on = json_on_value;
        }
        /* Keep global r,g,b in sync with per-pixel mode so JSON clients / UIs stay coherent. */
        if (json_pixels) {
            led_state.r = led_state.pix[0][0];
            led_state.g = led_state.pix[0][1];
            led_state.b = led_state.pix[0][2];
        } else if (json_one) {
            led_state.r = one_r;
            led_state.g = one_g;
            led_state.b = one_b;
        }
        /* Mirror hardware appearance into pix[] so GET /pixels matches solid & off states. */
        if (!led_state.on || led_state.preset == 0) {
            memset((void *)led_state.pix, 0, sizeof(led_state.pix));
        } else if (led_state.preset == 1) {
            for (int i = 0; i < LED_COUNT; i++) {
                led_state.pix[i][0] = led_state.r;
                led_state.pix[i][1] = led_state.g;
                led_state.pix[i][2] = led_state.b;
            }
        } else if (led_state.on && (led_state.preset == 10 || led_state.preset == 11)) {
            /* Pulse/fade need a non-empty palette; if strip was all-off, use r,g,b from the request. */
            bool any_lit = false;
            for (int i = 0; i < LED_COUNT; i++) {
                if (led_state.pix[i][0] || led_state.pix[i][1] || led_state.pix[i][2]) {
                    any_lit = true;
                    break;
                }
            }
            if (!any_lit) {
                for (int i = 0; i < LED_COUNT; i++) {
                    led_state.pix[i][0] = led_state.r;
                    led_state.pix[i][1] = led_state.g;
                    led_state.pix[i][2] = led_state.b;
                }
            }
        }
    }
    portEXIT_CRITICAL(&led_state_mux);
    cJSON_Delete(root);
    return led_send_state_json(req);
}

static esp_err_t bt_status_get_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
#if !CONFIG_BT_ENABLED
    return httpd_resp_send(req, "{\"enabled\":false,\"finding\":false,\"connected\":false,\"devices\":[]}", HTTPD_RESP_USE_STRLEN);
#else
    const size_t out_cap = 8192;
    char *out = (char *)malloc(out_cap);
    if (!out) return httpd_resp_send_500(req);
    int n = snprintf(out, out_cap, "{\"enabled\":true,\"finding\":%s,\"connecting\":%s,\"connected\":%s,\"auto_reconnect\":%s,\"bt_output_route\":%s,\"onboard_output\":%s,\"diag\":{\"disc_res_count\":%u,\"disc_start_count\":%u,\"disc_stop_count\":%u,\"disc_last_event_ms\":%u},\"connected_addr\":\"%s\",\"connected_name\":\"%s\",\"preferred_addr\":\"%s\",\"preferred_name\":\"%s\",\"devices\":[",
                     bt_finding ? "true" : "false",
                     bt_connecting ? "true" : "false",
                     bt_connected ? "true" : "false",
                     bt_auto_reconnect ? "true" : "false",
                     bt_output_route_active ? "true" : "false",
                     bt_output_route_active ? "false" : "true",
                     (unsigned)bt_disc_res_count,
                     (unsigned)bt_disc_start_count,
                     (unsigned)bt_disc_stop_count,
                     (unsigned)bt_disc_last_event_ms,
                     bt_connected_addr, bt_connected_name,
                     bt_preferred_addr, bt_preferred_name);
    if (n <= 0 || (size_t)n >= out_cap) {
        free(out);
        return httpd_resp_send_500(req);
    }
    bool first = true;
    portENTER_CRITICAL(&bt_mux);
    for (int i = 0; i < BT_MAX_DEVICES; i++) {
        if (!bt_devices[i].used) continue;
        int add = snprintf(out + n, out_cap - (size_t)n,
                           "%s{\"addr\":\"%s\",\"name\":\"%s\",\"rssi\":%d,\"last_seen_ms\":%u}",
                           first ? "" : ",",
                           bt_devices[i].addr,
                           bt_devices[i].name,
                           bt_devices[i].rssi,
                           (unsigned)bt_devices[i].last_seen_ms);
        first = false;
        if (add <= 0 || (size_t)(n + add) >= out_cap) {
            portEXIT_CRITICAL(&bt_mux);
            free(out);
            return httpd_resp_send_500(req);
        }
        n += add;
    }
    portEXIT_CRITICAL(&bt_mux);
    int tail = snprintf(out + n, out_cap - (size_t)n, "]}");
    if (tail <= 0 || (size_t)(n + tail) >= out_cap) {
        free(out);
        return httpd_resp_send_500(req);
    }
    n += tail;
    esp_err_t send_err = httpd_resp_send(req, out, (size_t)n);
    free(out);
    return send_err;
#endif
}

static esp_err_t bt_find_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
#if !CONFIG_BT_ENABLED
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"bt_not_enabled\"}", HTTPD_RESP_USE_STRLEN);
#else
    char body[256];
    int got = led_http_read_body(req, body, sizeof(body));
    if (got <= 0) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_body\"}", HTTPD_RESP_USE_STRLEN);
    cJSON *root = cJSON_Parse(body);
    if (!root) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_json\"}", HTTPD_RESP_USE_STRLEN);
    cJSON *j = cJSON_GetObjectItemCaseSensitive(root, "finding");
    bool on = cJSON_IsTrue(j);
    esp_err_t e = bt_set_finding(on);
    cJSON_Delete(root);
    if (e != ESP_OK) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"scan_toggle_failed\"}", HTTPD_RESP_USE_STRLEN);
    return httpd_resp_send(req, on ? "{\"ok\":true,\"finding\":true}" : "{\"ok\":true,\"finding\":false}", HTTPD_RESP_USE_STRLEN);
#endif
}

static esp_err_t bt_connect_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
#if !CONFIG_BT_ENABLED
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"bt_not_enabled\"}", HTTPD_RESP_USE_STRLEN);
#else
    char body[512];
    int got = led_http_read_body(req, body, sizeof(body));
    if (got <= 0) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_body\"}", HTTPD_RESP_USE_STRLEN);
    cJSON *root = cJSON_Parse(body);
    if (!root) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_json\"}", HTTPD_RESP_USE_STRLEN);
    cJSON *j_addr = cJSON_GetObjectItemCaseSensitive(root, "addr");
    cJSON *j_name = cJSON_GetObjectItemCaseSensitive(root, "name");
    const char *addr = cJSON_IsString(j_addr) ? j_addr->valuestring : "";
    const char *name = cJSON_IsString(j_name) ? j_name->valuestring : "";
    if (!addr || strlen(addr) < 11) {
        cJSON_Delete(root);
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_addr\"}", HTTPD_RESP_USE_STRLEN);
    }
    cJSON_Delete(root);
    bt_connect_req_t *job = (bt_connect_req_t *)calloc(1, sizeof(bt_connect_req_t));
    if (!job) {
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"oom\"}", HTTPD_RESP_USE_STRLEN);
    }
    strncpy(job->addr, addr, sizeof(job->addr) - 1);
    job->addr[sizeof(job->addr) - 1] = '\0';
    strncpy(job->name, name, sizeof(job->name) - 1);
    job->name[sizeof(job->name) - 1] = '\0';
    BaseType_t ok = xTaskCreatePinnedToCore(
        bt_connect_worker_task,
        "bt_connect_worker",
        4096,
        job,
        5,
        NULL,
        1
    );
    if (ok != pdPASS) {
        free(job);
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"connect_task_create_failed\"}", HTTPD_RESP_USE_STRLEN);
    }
    portENTER_CRITICAL(&bt_mux);
    bt_connecting = true;
    portEXIT_CRITICAL(&bt_mux);
    return httpd_resp_send(req, "{\"ok\":true,\"connecting\":true}", HTTPD_RESP_USE_STRLEN);
#endif
}

static esp_err_t bt_disconnect_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
#if !CONFIG_BT_ENABLED
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"bt_not_enabled\"}", HTTPD_RESP_USE_STRLEN);
#else
    if (bt_connected_addr[0] != '\0') {
        esp_bd_addr_t bda = {0};
        if (bt_str_to_addr(bt_connected_addr, bda)) {
            esp_a2d_source_disconnect(bda);
        }
    }
    bt_clear_connected_device();
    return httpd_resp_send(req, "{\"ok\":true,\"connected\":false,\"onboard_output\":true}", HTTPD_RESP_USE_STRLEN);
#endif
}

static esp_err_t bt_preferred_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
#if !CONFIG_BT_ENABLED
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"bt_not_enabled\"}", HTTPD_RESP_USE_STRLEN);
#else
    char body[512];
    int got = led_http_read_body(req, body, sizeof(body));
    if (got <= 0) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_body\"}", HTTPD_RESP_USE_STRLEN);
    cJSON *root = cJSON_Parse(body);
    if (!root) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_json\"}", HTTPD_RESP_USE_STRLEN);
    cJSON *j_addr = cJSON_GetObjectItemCaseSensitive(root, "addr");
    cJSON *j_name = cJSON_GetObjectItemCaseSensitive(root, "name");
    const char *addr = cJSON_IsString(j_addr) ? j_addr->valuestring : "";
    const char *name = cJSON_IsString(j_name) ? j_name->valuestring : "";
    if (!addr || strlen(addr) < 11) {
        cJSON_Delete(root);
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_addr\"}", HTTPD_RESP_USE_STRLEN);
    }
    bt_set_preferred_device(addr, name);
    cJSON_Delete(root);
    return httpd_resp_send(req, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
#endif
}

static esp_err_t bt_preferred_clear_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
#if !CONFIG_BT_ENABLED
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"bt_not_enabled\"}", HTTPD_RESP_USE_STRLEN);
#else
    bt_clear_preferred_device();
    return httpd_resp_send(req, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
#endif
}

static esp_err_t bt_auto_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
#if !CONFIG_BT_ENABLED
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"bt_not_enabled\"}", HTTPD_RESP_USE_STRLEN);
#else
    char body[256];
    int got = led_http_read_body(req, body, sizeof(body));
    if (got <= 0) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_body\"}", HTTPD_RESP_USE_STRLEN);
    cJSON *root = cJSON_Parse(body);
    if (!root) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_json\"}", HTTPD_RESP_USE_STRLEN);
    cJSON *j = cJSON_GetObjectItemCaseSensitive(root, "enabled");
    bt_auto_reconnect = cJSON_IsTrue(j);
    bt_pref_save(bt_preferred_addr, bt_preferred_name);
    cJSON_Delete(root);
    return httpd_resp_send(req, bt_auto_reconnect ? "{\"ok\":true,\"auto_reconnect\":true}" : "{\"ok\":true,\"auto_reconnect\":false}", HTTPD_RESP_USE_STRLEN);
#endif
}

static esp_err_t bt_api_options_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
    httpd_resp_set_status(req, "204 No Content");
    return httpd_resp_send(req, NULL, 0);
}

static esp_err_t audio_inject_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    if (!playback_ringbuf_local || !playback_ringbuf_bt) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"playback_unavailable\"}", HTTPD_RESP_USE_STRLEN);
    int declared = req->content_len;
    if (declared <= 0 || declared > (PLAYBACK_PUSH_MAX * (int)sizeof(int16_t))) {
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_content_length\"}", HTTPD_RESP_USE_STRLEN);
    }
    uint8_t *buf = (uint8_t *)malloc((size_t)declared);
    if (!buf) return httpd_resp_send(req, "{\"ok\":false,\"error\":\"alloc_failed\"}", HTTPD_RESP_USE_STRLEN);
    int got = 0;
    while (got < declared) {
        int r = httpd_req_recv(req, (char *)buf + got, (size_t)(declared - got));
        if (r <= 0) {
            free(buf);
            return httpd_resp_send(req, "{\"ok\":false,\"error\":\"recv_failed\"}", HTTPD_RESP_USE_STRLEN);
        }
        got += r;
    }
    if ((got % 2) != 0) got -= 1;
    if (got <= 0) {
        free(buf);
        return httpd_resp_send(req, "{\"ok\":false,\"error\":\"empty_payload\"}", HTTPD_RESP_USE_STRLEN);
    }
    inject_http_posts_total++;
    BaseType_t ok_local = xRingbufferSend(playback_ringbuf_local, buf, (size_t)got, pdMS_TO_TICKS(100));
    BaseType_t ok_bt = pdTRUE;
#if CONFIG_BT_ENABLED
    if (bt_output_route_active) {
        ok_bt = xRingbufferSend(playback_ringbuf_bt, buf, (size_t)got, pdMS_TO_TICKS(10));
    }
#endif
    inject_playback_deadline_ms = esp_log_timestamp() + 1500;
    free(buf);
    if (ok_local != pdTRUE || ok_bt != pdTRUE) {
        inject_http_posts_dropped++;
        return httpd_resp_send(req, "{\"ok\":true,\"dropped\":true}", HTTPD_RESP_USE_STRLEN);
    }
    inject_http_bytes_accepted += (uint32_t)got;
    return httpd_resp_send(req, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
}

static esp_err_t audio_inject_options_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "POST, OPTIONS");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
    httpd_resp_set_status(req, "204 No Content");
    return httpd_resp_send(req, NULL, 0);
}

static size_t drain_ringbuf_bytes(RingbufHandle_t rb) {
    if (!rb) return 0;
    size_t drained = 0;
    for (int i = 0; i < 2048; i++) {
        size_t item_size = 0;
        void *item = xRingbufferReceiveUpTo(rb, &item_size, 0, PLAYBACK_PUSH_MAX * sizeof(int16_t));
        if (!item) {
            break;
        }
        drained += item_size;
        vRingbufferReturnItem(rb, item);
    }
    return drained;
}

static esp_err_t audio_inject_flush_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    size_t local_drained = drain_ringbuf_bytes(playback_ringbuf_local);
    size_t bt_drained = 0;
#if CONFIG_BT_ENABLED
    bt_drained = drain_ringbuf_bytes(playback_ringbuf_bt);
#endif
    inject_playback_deadline_ms = 0;
    inject_http_bytes_accepted = inject_playback_bytes_consumed;
    inject_playback_fifo_level = 0;
    char out[180];
    snprintf(
        out,
        sizeof(out),
        "{\"ok\":true,\"flushed_local\":%u,\"flushed_bt\":%u}",
        (unsigned)local_drained,
        (unsigned)bt_drained
    );
    return httpd_resp_send(req, out, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t audio_inject_status_get_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    uint32_t accepted = inject_http_bytes_accepted;
    uint32_t consumed = inject_playback_bytes_consumed;
    uint32_t backlog = (accepted >= consumed) ? (accepted - consumed) : 0;
    char out[320];
    snprintf(
        out,
        sizeof(out),
        "{\"ok\":true,\"posts_total\":%u,\"posts_dropped\":%u,\"bytes_accepted\":%u,\"bytes_consumed\":%u,\"queue_bytes_est\":%u,\"fifo_level\":%u,\"underruns\":%u,\"deadline_ms\":%u,\"ts_ms\":%u}",
        (unsigned)inject_http_posts_total,
        (unsigned)inject_http_posts_dropped,
        (unsigned)accepted,
        (unsigned)consumed,
        (unsigned)backlog,
        (unsigned)inject_playback_fifo_level,
        (unsigned)inject_playback_underruns,
        (unsigned)inject_playback_deadline_ms,
        (unsigned)esp_log_timestamp()
    );
    return httpd_resp_send(req, out, HTTPD_RESP_USE_STRLEN);
}

#define AUDIO_JSON_POST_MAX 256

static esp_err_t audio_output_vol_options_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
    httpd_resp_set_status(req, "204 No Content");
    return httpd_resp_send(req, NULL, 0);
}

static esp_err_t audio_output_vol_get_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    int v = 100;
    esp_err_t err = esp_audio_get_play_vol(&v);
    if (err != ESP_OK) {
        v = 100;
    }
    char buf[48];
    snprintf(buf, sizeof(buf), "{\"volume\":%d}", v);
    return httpd_resp_send(req, buf, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t audio_output_vol_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    int declared = req->content_len;
    if (declared <= 0 || declared > AUDIO_JSON_POST_MAX) {
        return httpd_resp_send(req, "{\"error\":\"bad_content_length\"}", HTTPD_RESP_USE_STRLEN);
    }
    char *body = (char *)malloc((size_t)declared + 1);
    if (!body) {
        return httpd_resp_send(req, "{\"error\":\"oom\"}", HTTPD_RESP_USE_STRLEN);
    }
    int got = 0;
    while (got < declared) {
        int r = httpd_req_recv(req, (char *)body + got, (size_t)(declared - got));
        if (r <= 0) {
            free(body);
            return httpd_resp_send(req, "{\"error\":\"recv\"}", HTTPD_RESP_USE_STRLEN);
        }
        got += r;
    }
    body[got] = '\0';
    cJSON *root = cJSON_Parse(body);
    free(body);
    if (!root) {
        return httpd_resp_send(req, "{\"error\":\"invalid_json\"}", HTTPD_RESP_USE_STRLEN);
    }
    const cJSON *jv = cJSON_GetObjectItemCaseSensitive(root, "volume");
    int vol = 100;
    if (cJSON_IsNumber(jv)) {
        vol = (int)cJSON_GetNumberValue(jv);
    }
    cJSON_Delete(root);
    if (vol < 0) {
        vol = 0;
    }
    if (vol > 100) {
        vol = 100;
    }
    esp_audio_set_play_vol(vol);
    (void)esp_audio_get_play_vol(&vol);
    char out[56];
    snprintf(out, sizeof(out), "{\"ok\":true,\"volume\":%d}", vol);
    return httpd_resp_send(req, out, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t audio_push_cue_post_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(req, "application/json");
    led_start_breath_cue(255, 210, 0, LED_CUE_BREATH_MS);
    return httpd_resp_send(req, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
}

static esp_err_t audio_push_cue_options_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "POST, OPTIONS");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
    httpd_resp_set_status(req, "204 No Content");
    return httpd_resp_send(req, NULL, 0);
}

static void start_webserver(void) {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    /* We expose many API endpoints; default handler slots are too low and
       silently drop later registrations (causing 404 on valid routes). */
    config.max_uri_handlers = 40;
    config.stack_size = 12288;
    httpd_handle_t server = NULL;
    if (httpd_start(&server, &config) == ESP_OK) {
        httpd_uri_t audio = { .uri = "/api/audio/stream", .method = HTTP_GET, .handler = audio_stream_handler };
        httpd_uri_t led_get = { .uri = "/api/led", .method = HTTP_GET, .handler = led_api_get_handler };
        httpd_uri_t led_post = { .uri = "/api/led", .method = HTTP_POST, .handler = led_api_post_handler };
        httpd_uri_t led_opts = { .uri = "/api/led", .method = HTTP_OPTIONS, .handler = led_api_options_handler };
        httpd_uri_t bt_status = { .uri = "/api/bluetooth/status", .method = HTTP_GET, .handler = bt_status_get_handler };
        httpd_uri_t bt_find = { .uri = "/api/bluetooth/find", .method = HTTP_POST, .handler = bt_find_post_handler };
        httpd_uri_t bt_connect = { .uri = "/api/bluetooth/connect", .method = HTTP_POST, .handler = bt_connect_post_handler };
        httpd_uri_t bt_disconnect = { .uri = "/api/bluetooth/disconnect", .method = HTTP_POST, .handler = bt_disconnect_post_handler };
        httpd_uri_t bt_pref = { .uri = "/api/bluetooth/preferred", .method = HTTP_POST, .handler = bt_preferred_post_handler };
        httpd_uri_t bt_pref_clear = { .uri = "/api/bluetooth/preferred/clear", .method = HTTP_POST, .handler = bt_preferred_clear_post_handler };
        httpd_uri_t bt_auto = { .uri = "/api/bluetooth/auto", .method = HTTP_POST, .handler = bt_auto_post_handler };
        httpd_uri_t bt_opts_root = { .uri = "/api/bluetooth", .method = HTTP_OPTIONS, .handler = bt_api_options_handler };
        httpd_uri_t bt_opts_status = { .uri = "/api/bluetooth/status", .method = HTTP_OPTIONS, .handler = bt_api_options_handler };
        httpd_uri_t bt_opts_find = { .uri = "/api/bluetooth/find", .method = HTTP_OPTIONS, .handler = bt_api_options_handler };
        httpd_uri_t bt_opts_connect = { .uri = "/api/bluetooth/connect", .method = HTTP_OPTIONS, .handler = bt_api_options_handler };
        httpd_uri_t bt_opts_disconnect = { .uri = "/api/bluetooth/disconnect", .method = HTTP_OPTIONS, .handler = bt_api_options_handler };
        httpd_uri_t bt_opts_pref = { .uri = "/api/bluetooth/preferred", .method = HTTP_OPTIONS, .handler = bt_api_options_handler };
        httpd_uri_t bt_opts_pref_clear = { .uri = "/api/bluetooth/preferred/clear", .method = HTTP_OPTIONS, .handler = bt_api_options_handler };
        httpd_uri_t bt_opts_auto = { .uri = "/api/bluetooth/auto", .method = HTTP_OPTIONS, .handler = bt_api_options_handler };
        httpd_uri_t audio_inject = { .uri = "/api/audio/inject", .method = HTTP_POST, .handler = audio_inject_post_handler };
        httpd_uri_t audio_inject_opts = { .uri = "/api/audio/inject", .method = HTTP_OPTIONS, .handler = audio_inject_options_handler };
        httpd_uri_t audio_inject_flush = { .uri = "/api/audio/inject/flush", .method = HTTP_POST, .handler = audio_inject_flush_post_handler };
        httpd_uri_t audio_inject_flush_opts = { .uri = "/api/audio/inject/flush", .method = HTTP_OPTIONS, .handler = audio_inject_options_handler };
        httpd_uri_t audio_inject_status = { .uri = "/api/audio/inject/status", .method = HTTP_GET, .handler = audio_inject_status_get_handler };
        httpd_uri_t audio_out_vol_get = { .uri = "/api/audio/output-volume", .method = HTTP_GET, .handler = audio_output_vol_get_handler };
        httpd_uri_t audio_out_vol_post = { .uri = "/api/audio/output-volume", .method = HTTP_POST, .handler = audio_output_vol_post_handler };
        httpd_uri_t audio_out_vol_opts = { .uri = "/api/audio/output-volume", .method = HTTP_OPTIONS, .handler = audio_output_vol_options_handler };
        httpd_uri_t audio_push_cue = { .uri = "/api/audio/push-cue", .method = HTTP_POST, .handler = audio_push_cue_post_handler };
        httpd_uri_t audio_push_cue_opts = { .uri = "/api/audio/push-cue", .method = HTTP_OPTIONS, .handler = audio_push_cue_options_handler };
        httpd_uri_t net_status = { .uri = "/api/network/status", .method = HTTP_GET, .handler = network_status_get_handler };
        httpd_uri_t net_status_opts = { .uri = "/api/network/status", .method = HTTP_OPTIONS, .handler = network_status_options_handler };
        httpd_uri_t net_profiles = { .uri = "/api/network/profiles", .method = HTTP_POST, .handler = network_profiles_post_handler };
        httpd_uri_t net_profiles_opts = { .uri = "/api/network/profiles", .method = HTTP_OPTIONS, .handler = network_status_options_handler };
        #define REG_URI(u) do { \
            esp_err_t _re = httpd_register_uri_handler(server, &(u)); \
            if (_re != ESP_OK) ESP_LOGE(TAG, "uri register failed: %s (%d)", (u).uri, (int)_re); \
        } while (0)
        REG_URI(audio);
        REG_URI(led_get);
        REG_URI(led_post);
        REG_URI(led_opts);
        REG_URI(bt_status);
        REG_URI(bt_find);
        REG_URI(bt_connect);
        REG_URI(bt_disconnect);
        REG_URI(bt_pref);
        REG_URI(bt_pref_clear);
        REG_URI(bt_auto);
        REG_URI(audio_inject);
        REG_URI(audio_inject_flush);
        REG_URI(audio_inject_status);
        REG_URI(bt_opts_root);
        REG_URI(bt_opts_status);
        REG_URI(bt_opts_find);
        REG_URI(bt_opts_connect);
        REG_URI(bt_opts_disconnect);
        REG_URI(bt_opts_pref);
        REG_URI(bt_opts_pref_clear);
        REG_URI(bt_opts_auto);
        REG_URI(audio_inject_opts);
        REG_URI(audio_inject_flush_opts);
        REG_URI(audio_out_vol_get);
        REG_URI(audio_out_vol_post);
        REG_URI(audio_out_vol_opts);
        REG_URI(audio_push_cue);
        REG_URI(audio_push_cue_opts);
        REG_URI(net_status);
        REG_URI(net_status_opts);
        REG_URI(net_profiles);
        REG_URI(net_profiles_opts);
        #undef REG_URI
        ESP_LOGI(TAG, "HTTP server on port 80 (/api/audio/stream, /api/led, /api/network/status, /api/network/profiles)");
    }
}

static void playback_task(void *arg) {
    uint8_t *fifo = (uint8_t *)malloc(PLAYBACK_FIFO_BYTES);
    uint8_t *silence = (uint8_t *)calloc(1, PLAYBACK_FRAME_BYTES);
    uint8_t *frame_tmp = (uint8_t *)malloc(PLAYBACK_FRAME_BYTES);
    int16_t *stereo_frame = (int16_t *)malloc(PLAYBACK_STEREO_FRAME_BYTES);
    if (!fifo || !silence || !frame_tmp || !stereo_frame) {
        ESP_LOGE(TAG, "playback fifo alloc failed");
        while (1) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
    size_t fifo_len = 0;
    size_t fifo_head = 0;
    size_t fifo_tail = 0;
    bool primed = false;
    uint32_t next_frame_ms = 0;
    uint32_t underrun_log_at_ms = 0;
    uint32_t play_fail_log_at_ms = 0;
    while (1) {
        if (!playback_ringbuf_local) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        while (fifo_len < PLAYBACK_FIFO_BYTES) {
            size_t item_size = 0;
            void *item = xRingbufferReceiveUpTo(playback_ringbuf_local, &item_size, 0, PLAYBACK_PUSH_MAX * sizeof(int16_t));
            if (!item || item_size < sizeof(int16_t)) {
                break;
            }
            size_t room = PLAYBACK_FIFO_BYTES - fifo_len;
            size_t copy_len = item_size;
            if (copy_len > room) {
                copy_len = room;
            }
            if (copy_len > 0) {
                size_t first = PLAYBACK_FIFO_BYTES - fifo_tail;
                if (first > copy_len) {
                    first = copy_len;
                }
                memcpy(fifo + fifo_tail, item, first);
                size_t second = copy_len - first;
                if (second > 0) {
                    memcpy(fifo, ((const uint8_t *)item) + first, second);
                }
                fifo_tail = (fifo_tail + copy_len) % PLAYBACK_FIFO_BYTES;
                fifo_len += copy_len;
            }
            vRingbufferReturnItem(playback_ringbuf_local, item);
            if (copy_len < item_size) {
                break;
            }
        }
        inject_playback_fifo_level = (uint32_t)fifo_len;

        if (!primed) {
            if (fifo_len < PLAYBACK_PREBUFFER_BYTES) {
                /* Avoid starving IDLE1 while waiting for first playable prebuffer. */
                vTaskDelay(TICKS_AT_LEAST_1(8));
                continue;
            }
            primed = true;
            next_frame_ms = esp_log_timestamp();
            ESP_LOGI(TAG, "playback primed (%u bytes)", (unsigned)fifo_len);
        }

        uint32_t now_ms = esp_log_timestamp();
        if (now_ms < next_frame_ms) {
            vTaskDelay(pdMS_TO_TICKS(next_frame_ms - now_ms));
        } else if ((now_ms - next_frame_ms) > 80) {
            // If we got delayed for a while, re-sync pacing anchor.
            next_frame_ms = now_ms;
        }

        // If frame is just about to underrun, wait a few ms for late packets before filling silence.
        if (fifo_len < PLAYBACK_FRAME_BYTES) {
            size_t wait_item_size = 0;
            void *wait_item = xRingbufferReceiveUpTo(
                playback_ringbuf_local,
                &wait_item_size,
                TICKS_AT_LEAST_1(6),
                PLAYBACK_PUSH_MAX * sizeof(int16_t)
            );
            if (wait_item && wait_item_size >= sizeof(int16_t)) {
                size_t room = PLAYBACK_FIFO_BYTES - fifo_len;
                size_t copy_len = wait_item_size;
                if (copy_len > room) {
                    copy_len = room;
                }
                if (copy_len > 0) {
                    size_t first = PLAYBACK_FIFO_BYTES - fifo_tail;
                    if (first > copy_len) {
                        first = copy_len;
                    }
                    memcpy(fifo + fifo_tail, wait_item, first);
                    size_t second = copy_len - first;
                    if (second > 0) {
                        memcpy(fifo, ((const uint8_t *)wait_item) + first, second);
                    }
                    fifo_tail = (fifo_tail + copy_len) % PLAYBACK_FIFO_BYTES;
                    fifo_len += copy_len;
                }
                vRingbufferReturnItem(playback_ringbuf_local, wait_item);
            }
        }

        const bool have_audio_frame = (fifo_len >= PLAYBACK_FRAME_BYTES);
        const uint8_t *play_ptr = have_audio_frame ? frame_tmp : silence;
        if (have_audio_frame) {
            if ((fifo_head + PLAYBACK_FRAME_BYTES) <= PLAYBACK_FIFO_BYTES) {
                play_ptr = fifo + fifo_head;
            } else {
                size_t first = PLAYBACK_FIFO_BYTES - fifo_head;
                memcpy(frame_tmp, fifo + fifo_head, first);
                memcpy(frame_tmp + first, fifo, PLAYBACK_FRAME_BYTES - first);
                play_ptr = frame_tmp;
            }
        }
        const int16_t *mono_ptr = (const int16_t *)play_ptr;
        const int mono_samples = PLAYBACK_FRAME_BYTES / (int)sizeof(int16_t);
        for (int i = 0; i < mono_samples; i++) {
            const int16_t s = mono_ptr[i];
            stereo_frame[i * 2] = s;
            stereo_frame[i * 2 + 1] = s;
        }
        const uint8_t *out_ptr = (const uint8_t *)stereo_frame;
        const int total_bytes = PLAYBACK_STEREO_FRAME_BYTES;
        esp_err_t play_err = ESP_OK;
        for (int off = 0; off < total_bytes; ) {
            int chunk_bytes = total_bytes - off;
            if (chunk_bytes > PLAYBACK_STEREO_FRAME_BYTES) chunk_bytes = PLAYBACK_STEREO_FRAME_BYTES;
            play_err = esp_audio_play((const int16_t *)(out_ptr + off), chunk_bytes, pdMS_TO_TICKS(20));
            if (play_err != ESP_OK) break;
            off += chunk_bytes;
        }
        if (fifo_len >= PLAYBACK_FRAME_BYTES) {
            fifo_len -= PLAYBACK_FRAME_BYTES;
            fifo_head = (fifo_head + PLAYBACK_FRAME_BYTES) % PLAYBACK_FIFO_BYTES;
            inject_playback_bytes_consumed += PLAYBACK_FRAME_BYTES;
        } else {
            inject_playback_underruns++;
            if (now_ms >= underrun_log_at_ms) {
                underrun_log_at_ms = now_ms + 2000;
                ESP_LOGW(TAG, "playback underrun (fifo=%u bytes)", (unsigned)fifo_len);
            }
        }
        next_frame_ms += 20;
        if (play_err != ESP_OK) {
            if (now_ms >= play_fail_log_at_ms) {
                play_fail_log_at_ms = now_ms + 2000;
                ESP_LOGW(TAG, "inject playback write failed (speaker/aux): %s (%d)", esp_err_to_name(play_err), (int)play_err);
            }
        }
    }
}

void app_main(void) {
    ESP_LOGI(TAG, "Reset reason: %d", (int)esp_reset_reason());
    korvo_init_nvs();
    #if CONFIG_BT_ENABLED
    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    if (esp_bt_controller_init(&bt_cfg) == ESP_OK &&
        esp_bt_controller_enable(ESP_BT_MODE_BTDM) == ESP_OK &&
        esp_bluedroid_init() == ESP_OK &&
        esp_bluedroid_enable() == ESP_OK &&
        esp_bt_gap_register_callback(bt_gap_bt_cb) == ESP_OK &&
        esp_a2d_register_callback(bt_a2dp_cb) == ESP_OK &&
        esp_a2d_source_register_data_callback(bt_a2dp_data_cb) == ESP_OK &&
        esp_a2d_source_init() == ESP_OK &&
        esp_avrc_ct_init() == ESP_OK) {
        bt_ready = true;
        bt_pref_load();
        if (bt_auto_reconnect && bt_preferred_addr[0] != '\0') {
            esp_err_t ae = bt_set_finding(true);
            if (ae == ESP_OK) {
                ESP_LOGI(TAG, "Bluetooth auto-find started for preferred device: %s", bt_preferred_addr);
            } else {
                ESP_LOGW(TAG, "Bluetooth auto-find failed: %d", (int)ae);
            }
        }
        ESP_LOGI(TAG, "Bluetooth BLE ready");
    } else {
        bt_ready = false;
        ESP_LOGW(TAG, "Bluetooth BLE init failed");
    }
    #else
    ESP_LOGW(TAG, "Bluetooth disabled in build config (CONFIG_BT_ENABLED not set)");
    #endif
    ESP_LOGI(TAG, "=== BOOT START ===");
    
    ESP_LOGI(TAG, "Create ringbuf");
    audio_ringbuf = xRingbufferCreate(AUDIO_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    if (!audio_ringbuf) ESP_LOGE(TAG, "ringbuf failed");
    
    ESP_LOGI(TAG, "Create mutex");
    stream_mutex = xSemaphoreCreateMutex();
    if (!stream_mutex) ESP_LOGE(TAG, "mutex failed");
    playback_ringbuf_local = xRingbufferCreate(PLAYBACK_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    playback_ringbuf_bt = xRingbufferCreate(PLAYBACK_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    xTaskCreatePinnedToCore(playback_task, "playback_task", 6144, NULL, 5, NULL, 1);
    
    ESP_LOGI(TAG, "Setup LED state");
    led_state.on = true;
    led_state.preset = 2;
    led_state.b = 255;
    led_state.brightness = 56;
    
    ESP_LOGI(TAG, "Create LED task");
    xTaskCreatePinnedToCore(led_task, "led_task", 4096, NULL, 5, NULL, 1);
    vTaskDelay(pdMS_TO_TICKS(300));
    ESP_LOGI(TAG, "LED task created");

    if (strlen(KORVO_WIFI_SSID) == 0) {
        ESP_LOGW(TAG, "Build-time WiFi is empty; runtime profiles can still be provisioned via /api/network/profiles.");
    }

    ESP_LOGI(TAG, "Init WiFi");
    wifi_init_sta();

    ESP_LOGI(TAG, "Wait for WiFi...");
    int waited = 0;
    while (!wifi_connected && waited < 30000) {
        vTaskDelay(pdMS_TO_TICKS(500));
        waited += 500;
    }
    ESP_LOGI(TAG, "WiFi check done: %s (%d ms)", wifi_connected ? "OK" : "FAIL", waited);

    const bool sta_has_ip = wifi_connected;
    if (!sta_has_ip) {
        wifi_start_softap_fallback();
        /* SoftAP up: brief cyan breath (then ring off until API/dashboard sets LEDs). */
        led_start_breath_cue(0, 180, 255, LED_CUE_BREATH_MS);
    }

    vTaskDelay(pdMS_TO_TICKS(1000));
    ESP_LOGI(TAG, "Start webserver...");
    start_webserver();
    vTaskDelay(pdMS_TO_TICKS(100));

    esp_err_t board_init = esp_board_init(AUDIO_SAMPLE_RATE, 2, AUDIO_BIT_DEPTH);
    ESP_LOGI(TAG, "Audio init result: %d", board_init);
    if (board_init == ESP_OK) {
        esp_err_t v = esp_audio_set_play_vol(100);
        if (v != ESP_OK) {
            ESP_LOGW(TAG, "esp_audio_set_play_vol(100) failed: %s", esp_err_to_name(v));
        }
    }
    if (sta_has_ip) {
        ESP_LOGI(TAG, "Waiting for WiFi animation to finish...");
        uint32_t wait_ms = 0;
        while (wait_ms < 6000) {
            int pr;
            portENTER_CRITICAL(&led_state_mux);
            pr = led_state.preset;
            portEXIT_CRITICAL(&led_state_mux);
            if (pr == 0) {
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(100));
            wait_ms += 100;
        }
    } else {
        ESP_LOGI(TAG, "SoftAP mode — skipping STA success LED sequence");
        vTaskDelay(pdMS_TO_TICKS(800));
    }

    ESP_LOGI(TAG, "=== AUDIO LOOP START ===");
    const int feed_ch = esp_get_feed_channel();
    ESP_LOGI(TAG, "Mic feed channels: %d (stream is mono downmix)", feed_ch);
    const size_t raw_bytes = (size_t)AUDIO_CHUNK_SIZE * sizeof(int16_t) * (size_t)feed_ch;
    /* ES7210 TDM path: esp_get_feed_channel() is 4 (AFE mics) but esp_codec_dev_read fills
     * stereo int32 (same byte count as 4×int16). Treating that buffer as int16 was silent. */
    const bool raw_is_s32_stereo = (raw_bytes == (size_t)AUDIO_CHUNK_SIZE * 2u * sizeof(int32_t));
    ESP_LOGI(TAG, "Mic PCM path: raw_bytes=%u raw_is_s32_stereo=%d i32_to_s16_shift=%d (AUDIO diag when streaming)",
             (unsigned)raw_bytes, (int)raw_is_s32_stereo, KORVO_MIC_I32_MONO_SHIFT);
    void *raw_buf = malloc(raw_bytes);
    int16_t *mono_buf = (int16_t *)malloc((size_t)AUDIO_CHUNK_SIZE * sizeof(int16_t));
    if (!raw_buf || !mono_buf) {
        ESP_LOGE(TAG, "audio buffer alloc failed (raw=%p mono=%p)", raw_buf, (void *)mono_buf);
        while (1) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
    bool was_streaming = false;
    while (1) {
        /* Keep microphone capture running continuously even during TTS inject playback.
         * Echo suppression is handled server-side; hard-pausing capture here can starve ASR.
         */
        if (!stream_client_connected) {
            if (was_streaming) {
                was_streaming = false;
                /* Yellow breath moment on stream end (then off; no sustained WiFi/SoftAP animation). */
                led_start_breath_cue(255, 210, 0, LED_CUE_BREATH_MS);
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        esp_err_t result = esp_get_feed_data(true, (int16_t *)raw_buf, (int)raw_bytes);
        if (stream_client_connected && !was_streaming) {
            was_streaming = true;
            led_start_breath_cue(255, 210, 0, LED_CUE_BREATH_MS);
            ESP_LOGI(TAG, "HTTP stream client connected — serial: AUDIO diag every 2s (mono |s16|; if s32 path, I32 L/R peaks)");
        }

        if (result == ESP_OK && stream_client_connected && audio_ringbuf) {
            if (raw_is_s32_stereo) {
                /* ES7210 + esp_codec_dev_read: stereo int32 (upper bits carry level; clamp-only was rail distortion). */
                const int32_t *s32 = (const int32_t *)raw_buf;
                for (int i = 0; i < AUDIO_CHUNK_SIZE; i++) {
                    int64_t L = (int64_t)s32[i * 2];
                    int64_t R = (int64_t)s32[i * 2 + 1];
                    int64_t m = (L + R) / 2;
                    m >>= KORVO_MIC_I32_MONO_SHIFT;
                    if (m > 32767) {
                        m = 32767;
                    }
                    if (m < -32768) {
                        m = -32768;
                    }
                    mono_buf[i] = (int16_t)m;
                }
            } else {
                const int16_t *s16 = (const int16_t *)raw_buf;
                for (int i = 0; i < AUDIO_CHUNK_SIZE; i++) {
                    int32_t acc = 0;
                    for (int c = 0; c < feed_ch; c++) {
                        acc += (int32_t)s16[i * feed_ch + c];
                    }
                    mono_buf[i] = (int16_t)(acc / feed_ch);
                }
            }
            /* Serial monitor: mic energy (authoritative vs host-side WAV scripts). */
            {
                static uint32_t pcm_diag_next_ms;
                uint32_t now_ms = esp_log_timestamp();
                if (now_ms >= pcm_diag_next_ms) {
                    pcm_diag_next_ms = now_ms + 2000;
                    int16_t mono_max = 0;
                    for (int i = 0; i < AUDIO_CHUNK_SIZE; i++) {
                        int16_t v = mono_buf[i];
                        int16_t a = (v < 0) ? (int16_t)-v : v;
                        if (a > mono_max) {
                            mono_max = a;
                        }
                    }
                    if (raw_is_s32_stereo) {
                        const int32_t *s32r = (const int32_t *)raw_buf;
                        int32_t l_max = 0, r_max = 0;
                        for (int i = 0; i < AUDIO_CHUNK_SIZE; i++) {
                            int32_t L = s32r[i * 2];
                            int32_t R = s32r[i * 2 + 1];
                            int32_t al = (L < 0) ? -L : L;
                            int32_t ar = (R < 0) ? -R : R;
                            if (al > l_max) {
                                l_max = al;
                            }
                            if (ar > r_max) {
                                r_max = ar;
                            }
                        }
                        ESP_LOGI(TAG, "AUDIO diag: mono_max=%d I32_L_max=%d I32_R_max=%d feed_ch=%d",
                                 (int)mono_max, (int)l_max, (int)r_max, feed_ch);
                    } else {
                        ESP_LOGI(TAG, "AUDIO diag: mono_max=%d path=int16_mc feed_ch=%d", (int)mono_max, feed_ch);
                    }
                }
            }
            if (xSemaphoreTake(stream_mutex, TICKS_AT_LEAST_1(5))) {
                if (xRingbufferSend(audio_ringbuf, mono_buf, (size_t)AUDIO_CHUNK_SIZE * sizeof(int16_t), TICKS_AT_LEAST_1(5)) != pdTRUE) {
                    static uint32_t rb_drop;
                    if ((++rb_drop % 125u) == 0u) {
                        ESP_LOGW(TAG, "audio ringbuf full — dropping chunks (HTTP client slow?)");
                    }
                }
                xSemaphoreGive(stream_mutex);
            }
        } else if (result != ESP_OK && stream_client_connected) {
            static uint32_t feed_err;
            if ((++feed_err % 500u) == 0u) {
                ESP_LOGW(TAG, "esp_get_feed_data failed while streaming: %d", (int)result);
            }
        }
        /* Keep CPU0 schedulable under mixed stream+inject pressure (prevents IDLE0 watchdog trips). */
        vTaskDelay(TICKS_AT_LEAST_1(2));
    }
}
