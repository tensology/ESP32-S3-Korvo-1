#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "esp_http_server.h"
#include "mdns.h"
#include "led_strip.h"
#include "esp_board_init.h"
#include "korvo_config.h"

#define AUDIO_RINGBUF_SIZE (64 * 1024)
#define AUDIO_SAMPLE_RATE 16000
#define AUDIO_BIT_DEPTH 16
#define AUDIO_CHUNK_SIZE 640
#define AUDIO_CHUNK_BYTES ((size_t)AUDIO_CHUNK_SIZE * sizeof(int16_t))
/* ES7210 + I2S int32 stereo: energy is in the upper int32 range; scale before int16 clamp.
 * RECORD_VOLUME in board header sets PGA only. Increase shift if still clipped; decrease if too quiet. */
#ifndef KORVO_MIC_I32_MONO_SHIFT
#define KORVO_MIC_I32_MONO_SHIFT 14
#endif

static RingbufHandle_t audio_ringbuf = NULL;
static volatile bool stream_client_connected = false;
static SemaphoreHandle_t stream_mutex = NULL;

static const char *TAG = "KORVO";

#define LED_GPIO    33
#define LED_COUNT   12

static volatile struct {
    bool on;
    int preset;
    uint8_t r, g, b;
    uint8_t brightness;
    int auto_off_ms;
} led_state = {0};

static led_strip_handle_t strip = NULL;

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
        struct { bool on; int preset; uint8_t r,g,b; uint8_t brightness; int auto_off_ms; } s;
        s.on = led_state.on;
        s.preset = led_state.preset;
        s.r = led_state.r;
        s.g = led_state.g;
        s.b = led_state.b;
        s.brightness = led_state.brightness;
        s.auto_off_ms = led_state.auto_off_ms;

        if (s.auto_off_ms > 0 && s.on) {
            led_state.auto_off_ms = 0;
            s.auto_off_ms = 0;
        }

        if (!s.on || s.preset == 0) {
            anim_clear();
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        switch (s.preset) {
            case 1: anim_solid(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(50)); break;
            case 2: anim_spinner(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(60)); break;
            case 4: anim_breathe(s.r, s.g, s.b, s.brightness); vTaskDelay(pdMS_TO_TICKS(25)); break;
            case 10: {
                for(int i=0; i<=255; i+=5) { anim_solid(0, 255, 0, i); vTaskDelay(pdMS_TO_TICKS(10)); }
                vTaskDelay(pdMS_TO_TICKS(200));
                for(int i=255; i>=0; i-=5) { anim_solid(0, 255, 0, i); vTaskDelay(pdMS_TO_TICKS(10)); }
                vTaskDelay(pdMS_TO_TICKS(200));
                for(int i=0; i<=255; i+=5) { anim_solid(0, 255, 0, i); vTaskDelay(pdMS_TO_TICKS(10)); }
                vTaskDelay(pdMS_TO_TICKS(200));
                for(int i=255; i>=0; i-=5) { anim_solid(0, 255, 0, i); vTaskDelay(pdMS_TO_TICKS(10)); }
                anim_clear();
                led_state.preset = 0;
                led_state.on = false;
                break;
            }
            case 11: {
                for(int i=0; i<=255; i+=10) { anim_solid(255, 200, 0, i); vTaskDelay(pdMS_TO_TICKS(10)); }
                vTaskDelay(pdMS_TO_TICKS(2000));
                for(int i=255; i>=0; i-=5) { anim_solid(255, 200, 0, i); vTaskDelay(pdMS_TO_TICKS(20)); }
                anim_clear();
                led_state.preset = 0;
                led_state.on = false;
                break;
            }
            default: anim_clear(); vTaskDelay(pdMS_TO_TICKS(50)); break;
        }
    }
}

static volatile bool wifi_connected = false;
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
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
        led_state.on = true;
        led_state.preset = 10;
    }
}

static void wifi_init_sta(void)
{
    ESP_LOGI(TAG, "Connecting to: %s", KORVO_WIFI_SSID);
    if (nvs_flash_init() != ESP_OK) nvs_flash_erase();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_event_handler_instance_t inst_any, inst_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &inst_any));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &inst_ip));
    wifi_config_t wifi_config = {0};
    strncpy((char *)wifi_config.sta.ssid, KORVO_WIFI_SSID, 32);
    strncpy((char *)wifi_config.sta.password, KORVO_WIFI_PASSWORD, 64);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
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
            if (httpd_resp_send_chunk(req, (const char *)buffer, item_size) != ESP_OK) {
                break;
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

static void start_webserver(void) {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    httpd_handle_t server = NULL;
    if (httpd_start(&server, &config) == ESP_OK) {
        httpd_uri_t audio = { .uri="/api/audio/stream", .method=HTTP_GET, .handler=audio_stream_handler };
        httpd_register_uri_handler(server, &audio);
        ESP_LOGI(TAG, "HTTP server on port 80");
    }
}

void app_main(void) {
    ESP_LOGI(TAG, "=== BOOT START ===");
    
    ESP_LOGI(TAG, "Create ringbuf");
    audio_ringbuf = xRingbufferCreate(AUDIO_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    if (!audio_ringbuf) ESP_LOGE(TAG, "ringbuf failed");
    
    ESP_LOGI(TAG, "Create mutex");
    stream_mutex = xSemaphoreCreateMutex();
    if (!stream_mutex) ESP_LOGE(TAG, "mutex failed");
    
    ESP_LOGI(TAG, "Setup LED state");
    led_state.on = true;
    led_state.preset = 2;
    led_state.b = 255;
    led_state.brightness = 200;
    
    ESP_LOGI(TAG, "Create LED task");
    xTaskCreatePinnedToCore(led_task, "led_task", 4096, NULL, 5, NULL, 1);
    vTaskDelay(pdMS_TO_TICKS(300));
    ESP_LOGI(TAG, "LED task created");

    if (strlen(KORVO_WIFI_SSID) == 0) {
        ESP_LOGW(TAG, "KORVO_WIFI_SSID is empty — add/activate WiFi in Korvo server UI, then flash so korvo_config.h is updated.");
        led_state.on = true;
        led_state.preset = 1;
        led_state.r = 255;
        led_state.g = 0;
        led_state.b = 0;
        led_state.brightness = 200;
        while (1) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
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

    if (wifi_connected) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        ESP_LOGI(TAG, "Start webserver...");
        start_webserver();
        vTaskDelay(pdMS_TO_TICKS(100));
        
        ESP_LOGI(TAG, "Waiting for WiFi animation to finish...");
        while (led_state.preset != 0) {
            vTaskDelay(pdMS_TO_TICKS(100));
        }
        
        ESP_LOGI(TAG, "Audio init...");
        esp_err_t board_init = esp_board_init(AUDIO_SAMPLE_RATE, 1, AUDIO_BIT_DEPTH);
        ESP_LOGI(TAG, "Audio init result: %d", board_init);
        
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
            esp_err_t result = esp_get_feed_data(true, (int16_t *)raw_buf, (int)raw_bytes);
            if (stream_client_connected && !was_streaming) {
                was_streaming = true;
                led_state.on = true;
                led_state.preset = 11;
                ESP_LOGI(TAG, "HTTP stream client connected — serial: AUDIO diag every 2s (mono |s16|; if s32 path, I32 L/R peaks)");
            } else if (!stream_client_connected && was_streaming) {
                was_streaming = false;
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
                if (xSemaphoreTake(stream_mutex, pdMS_TO_TICKS(5))) {
                    if (xRingbufferSend(audio_ringbuf, mono_buf, (size_t)AUDIO_CHUNK_SIZE * sizeof(int16_t), pdMS_TO_TICKS(5)) != pdTRUE) {
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
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    } else {
        ESP_LOGI(TAG, "WiFi failed - restart");
        led_state.on = true;
        led_state.preset = 1;
        led_state.r = 255;
        vTaskDelay(pdMS_TO_TICKS(3000));
        esp_restart();
    }
}
