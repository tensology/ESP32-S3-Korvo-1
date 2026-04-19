/*
   Korvo Wake Word Agent with Real Audio
   =====================================
   Boot      → blue spinner (loading)
   WiFi OK   → green glow 3s → off
   WiFi fail → red glow 5s → off
   Wake word → blue glow then off
   HTTP API  → LED control from web UI
   Audio stream → Connect audio endpoint and press play to stream mic

   RULE: Only the LED task touches the LED strip hardware.
   Everything else writes to the volatile led_state struct.
   No mutex - small struct, atomic-ish writes on ESP32.

   FIX: Wake word model init runs on core 0 in its own task
   to avoid INT WDT timeout on core 1 during PSRAM cache lock.
*/
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "esp_http_server.h"
#include "esp_timer.h"
#include "cJSON.h"
#include "led_strip.h"
#include "korvo_config.h"

#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "model_path.h"
#include "hiesp.h"
#include "hilexin.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_board_init.h"
#include "driver/i2s.h"
#include "esp_afe_config.h"  // Need for enums

#define AUDIO_RINGBUF_SIZE (64 * 1024)  // 64KB ring buffer for streaming
#define AUDIO_SAMPLE_RATE 16000
#define AUDIO_BIT_DEPTH 16
#define AUDIO_CHANNEL_NUM 1    // mono output for browser
#define AUDIO_CHUNK_SIZE 640   // 20ms chunks at 16kHz mono

// Audio streaming globals
static RingbufHandle_t audio_ringbuf = NULL;
static volatile bool stream_client_connected = false;
static SemaphoreHandle_t stream_mutex = NULL;

static const char *TAG = "KORVO";

// ─── LED Strip ───
#define LED_GPIO    33
#define LED_COUNT   12

typedef struct {
    uint8_t r, g, b;
    bool on;
    int preset;        // 0=off, 1=solid, 2=spinner, 3=rainbow, 4=breathe, 5=chase
    uint8_t brightness;
    int auto_off_ms;   // 0=never auto-off, >0=ms until auto-off
} led_state_t;

static volatile led_state_t led_state = {0};
static led_strip_handle_t strip = NULL;

static void led_go(bool on, int preset, uint8_t r, uint8_t g, uint8_t b, int auto_off_ms) {
    led_state.on = on;
    led_state.preset = preset;
    led_state.r = r;
    led_state.g = g;
    led_state.b = b;
    if (on && led_state.brightness == 0) led_state.brightness = 200;
    led_state.auto_off_ms = auto_off_ms;
}

// ─── LED Animations (only called from led_task) ───
static void anim_clear(void) {
    led_strip_clear(strip);
    led_strip_refresh(strip);
}

static void anim_solid(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    float s = bright / 255.0f;
    for (int i = 0; i < LED_COUNT; i++)
        led_strip_set_pixel(strip, i, (uint8_t)(r*s), (uint8_t)(g*s), (uint8_t)(b*s));
    led_strip_refresh(strip);
}

static void anim_spinner(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    static int pos = 0;
    led_strip_clear(strip);
    float s = bright / 255.0f;
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

static void anim_rainbow(uint8_t bright) {
    static int offset = 0;
    float s = bright / 255.0f;
    for (int i = 0; i < LED_COUNT; i++) {
        int hue = (i * 360 / LED_COUNT + offset) % 360;
        int h = hue / 60, f = hue % 60;
        uint8_t r, g, b;
        switch (h) {
            case 0: r=255; g=f*255/60; b=0; break;
            case 1: r=255-f*255/60; g=255; b=0; break;
            case 2: r=0; g=255; b=f*255/60; break;
            case 3: r=0; g=255-f*255/60; b=255; break;
            case 4: r=f*255/60; g=0; b=255; break;
            default: r=255; g=0; b=255-f*255/60; break;
        }
        led_strip_set_pixel(strip, i, (uint8_t)(r*s), (uint8_t)(g*s), (uint8_t)(b*s));
    }
    led_strip_refresh(strip);
    offset = (offset + 3) % 360;
}

static void anim_breathe(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    static int step = 0, dir = 1;
    float s = bright / 255.0f;
    float level = step / 100.0f;
    for (int i = 0; i < LED_COUNT; i++)
        led_strip_set_pixel(strip, i,
            (uint8_t)(r * level * s), (uint8_t)(g * level * s), (uint8_t)(b * level * s));
    led_strip_refresh(strip);
    step += dir * 2;
    if (step >= 100 || step <= 0) dir *= -1;
}

static void anim_chase(uint8_t r, uint8_t g, uint8_t b, uint8_t bright) {
    static int pos = 0;
    led_strip_clear(strip);
    float s = bright / 255.0f;
    for (int k = 0; k < 3; k++) {
        int idx = (pos + k) % LED_COUNT;
        float fade = (3 - k) / 3.0f;
        led_strip_set_pixel(strip, idx,
            (uint8_t)(r * fade * s), (uint8_t)(g * fade * s), (uint8_t)(b * fade * s));
    }
    led_strip_refresh(strip);
    pos = (pos + 1) % LED_COUNT;
}

static volatile bool led_task_ready = false;

static void led_task(void *arg) {
    const led_strip_config_t led_config = {
        .strip_gpio_num = LED_GPIO,
        .max_leds = LED_COUNT,
        .led_pixel_format = LED_PIXEL_FORMAT_GRB,
        .led_model = LED_MODEL_WS2812,
    };
    const led_strip_rmt_config_t rmt_config = {};
    if (led_strip_new_rmt_device(&led_config, &rmt_config, &strip) != ESP_OK || !strip) {
        ESP_LOGE(TAG, "WS2812 init failed on GPIO %d", LED_GPIO);
        vTaskDelete(NULL);
        return;
    }
    anim_clear();
    ESP_LOGI(TAG, "WS2812: %d LEDs on GPIO %d", LED_COUNT, LED_GPIO);
    led_task_ready = true;

    int64_t auto_off_time = 0;

    while (1) {
        led_state_t s;
        s.on = led_state.on;
        s.preset = led_state.preset;
        s.r = led_state.r;
        s.g = led_state.g;
        s.b = led_state.b;
        s.brightness = led_state.brightness;
        s.auto_off_ms = led_state.auto_off_ms;

        if (s.auto_off_ms > 0 && s.on) {
            auto_off_time = esp_timer_get_time() / 1000 + s.auto_off_ms;
            led_state.auto_off_ms = 0;
        }
        if (s.on && auto_off_time > 0) {
            int64_t now = esp_timer_get_time() / 1000;
            if (now >= auto_off_time) {
                led_go(false, 0, 0, 0, 0, 0);
                auto_off_time = 0;
                continue;
            }
        }

        if (!s.on || s.preset == 0) {
            anim_clear();
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        switch (s.preset) {
            case 1: anim_solid(s.r, s.g, s.b, s.brightness);    vTaskDelay(pdMS_TO_TICKS(50));  break;
            case 2: anim_spinner(s.r, s.g, s.b, s.brightness);  vTaskDelay(pdMS_TO_TICKS(60));  break;
            case 3: anim_rainbow(s.brightness);                   vTaskDelay(pdMS_TO_TICKS(40));  break;
            case 4: anim_breathe(s.r, s.g, s.b, s.brightness);  vTaskDelay(pdMS_TO_TICKS(25));  break;
            case 5: anim_chase(s.r, s.g, s.b, s.brightness);    vTaskDelay(pdMS_TO_TICKS(70));  break;
            default: anim_clear();                                vTaskDelay(pdMS_TO_TICKS(50));  break;
        }
    }
}

// ─── WiFi ───
static volatile bool wifi_connected = false;
static int retry_count = 0;
#define MAX_RETRY 10

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
        ESP_LOGI(TAG, "WiFi connecting...");
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_connected = false;
        wifi_event_sta_disconnected_t *disconn = (wifi_event_sta_disconnected_t *)event_data;
        ESP_LOGW(TAG, "WiFi disconnected reason: %d", disconn->reason);
        if (retry_count < MAX_RETRY) {
            esp_wifi_connect();
            retry_count++;
            ESP_LOGI(TAG, "WiFi retry %d/%d", retry_count, MAX_RETRY);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "WiFi connected! IP: " IPSTR, IP2STR(&event->ip_info.ip));
        wifi_connected = true;
        retry_count = 0;
        led_go(true, 1, 0, 220, 50, 3000);  // solid green 3s then off
    }
}

static void wifi_init_sta(void)
{
    ESP_LOGI(TAG, "Connecting to SSID: %s", KORVO_WIFI_SSID);
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NOT_FOUND || ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS init failed (0x%x), formatting...", ret);
        ret = nvs_flash_erase();
        if (ret == ESP_OK) ret = nvs_flash_init();
    }
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "NVS unavailable (0x%x), WiFi disabled", ret);
        led_go(true, 1, 255, 50, 0, 5000);  // solid red 5s then off
        return;
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t inst_any, inst_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &inst_any));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &inst_ip));

    wifi_config_t wifi_config = {0};
    strncpy((char *)wifi_config.sta.ssid, KORVO_WIFI_SSID, sizeof(wifi_config.sta.ssid) - 1);
    strncpy((char *)wifi_config.sta.password, KORVO_WIFI_PASSWORD, sizeof(wifi_config.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
}

// ─── HTTP Server ───
static esp_err_t led_post_handler(httpd_req_t *req) {
    char buf[256];
    int ret = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (ret <= 0) { httpd_resp_send_500(req); return ESP_FAIL; }
    buf[ret] = '\0';

    cJSON *root = cJSON_Parse(buf);
    if (!root) { httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON"); return ESP_FAIL; }

    led_state_t s;
    s.on = led_state.on;
    s.preset = led_state.preset;
    s.r = led_state.r;
    s.g = led_state.g;
    s.b = led_state.b;
    s.brightness = led_state.brightness;

    cJSON *j;
    if ((j = cJSON_GetObjectItem(root, "on")))       s.on = cJSON_IsTrue(j);
    if ((j = cJSON_GetObjectItem(root, "r")))        s.r = j->valueint;
    if ((j = cJSON_GetObjectItem(root, "g")))        s.g = j->valueint;
    if ((j = cJSON_GetObjectItem(root, "b")))        s.b = j->valueint;
    if ((j = cJSON_GetObjectItem(root, "preset")))   s.preset = j->valueint;
    if ((j = cJSON_GetObjectItem(root, "brightness"))) s.brightness = j->valueint;
    led_go(s.on, s.preset, s.r, s.g, s.b, 0);
    cJSON_Delete(root);

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddBoolToObject(resp, "on", s.on);
    cJSON_AddNumberToObject(resp, "r", s.r);
    cJSON_AddNumberToObject(resp, "g", s.g);
    cJSON_AddNumberToObject(resp, "b", s.b);
    cJSON_AddNumberToObject(resp, "preset", s.preset);
    cJSON_AddNumberToObject(resp, "brightness", s.brightness);
    cJSON_AddBoolToObject(resp, "wifi", wifi_connected);

    char *json = cJSON_Print(resp);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_sendstr(req, json);
    cJSON_free(json);
    cJSON_Delete(resp);
    return ESP_OK;
}

static esp_err_t led_get_handler(httpd_req_t *req) {
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddBoolToObject(resp, "on", led_state.on);
    cJSON_AddNumberToObject(resp, "r", led_state.r);
    cJSON_AddNumberToObject(resp, "g", led_state.g);
    cJSON_AddNumberToObject(resp, "b", led_state.b);
    cJSON_AddNumberToObject(resp, "preset", led_state.preset);
    cJSON_AddNumberToObject(resp, "brightness", led_state.brightness);
    cJSON_AddBoolToObject(resp, "wifi", wifi_connected);

    char *json = cJSON_Print(resp);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_sendstr(req, json);
    cJSON_free(json);
    cJSON_Delete(resp);
    return ESP_OK;
}

static esp_err_t cors_handler(httpd_req_t *req) {
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
    httpd_resp_sendstr(req, "");
    return ESP_OK;
}

static esp_err_t status_handler(httpd_req_t *req) {
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "name", "korvo");
    cJSON_AddBoolToObject(resp, "wifi", wifi_connected);
    cJSON_AddBoolToObject(resp, "on", led_state.on);
    cJSON_AddNumberToObject(resp, "preset", led_state.preset);
    cJSON_AddNumberToObject(resp, "brightness", led_state.brightness);

    char *json = cJSON_Print(resp);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_sendstr(req, json);
    cJSON_free(json);
    cJSON_Delete(resp);
    return ESP_OK;
}

// HTTP handler for audio stream
static esp_err_t audio_stream_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "audio/wav");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=mic.wav");
    httpd_resp_set_hdr(req, "transferMode.dlna.org", "Streaming");
    httpd_resp_set_hdr(req, "contentFeatures.dlna.org", "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000");

    // Indicate streaming content
    httpd_resp_set_status(req, "200 OK");

    stream_client_connected = true;

    // Send a simple WAV header for our stream
    unsigned char wav_header[] = {
        'R', 'I', 'F', 'F',   // ChunkID
        0xFF, 0xFF, 0xFF, 0xFF,  // ChunkSize (will be 0xFFFFFFFF for streaming)
        'W', 'A', 'V', 'E',   // Format
        'f', 'm', 't', ' ',   // Subchunk1ID
        0x10, 0x00, 0x00, 0x00,  // Subchunk1Size (16 for PCM)
        0x01, 0x00,          // AudioFormat (1 = PCM)
        0x01, 0x00,          // NumChannels (Mono = 1)
        0x80, 0x3E, 0x00, 0x00,  // SampleRate (16000 Hz)
        0x00, 0x7D, 0x00, 0x00,  // ByteRate (16000 * 1 * 16 / 8 = 32000)
        0x02, 0x00,          // BlockAlign (1 * 16 / 8 = 2)
        0x10, 0x00,          // BitsPerSample (16-bit)
        'd', 'a', 't', 'a',   // Subchunk2ID
        0xFE, 0xFF, 0xFF, 0xFF   // Subchunk2Size (0xFFFFFFFE -> unknown size for streaming)
    };

    httpd_resp_send_chunk(req, (const char*)wav_header, sizeof(wav_header));

    // Receive buffer to read from ringbuf and send via HTTP
    unsigned char *buffer = (unsigned char *)malloc(AUDIO_CHUNK_SIZE*4);  // Allow extra space

    if (!buffer) {
        ESP_LOGE(TAG, "Failed to allocate audio stream buffer");
        stream_client_connected = false;
        return ESP_ERR_NO_MEM;
    }

    while (stream_client_connected) {
        size_t item_size;
        void *item = NULL;

        if (stream_mutex && xSemaphoreTake(stream_mutex, portMAX_DELAY)) {
            item = xRingbufferReceive(audio_ringbuf, &item_size, pdMS_TO_TICKS(1000));
            if (item && item_size > 0) {
                // Copy data to local buffer
                if (item_size > AUDIO_CHUNK_SIZE*4) item_size = AUDIO_CHUNK_SIZE*4;
                memcpy(buffer, item, item_size);

                // Return the ringbuffer item (needed after xRingbufferReceive)
                vRingbufferReturnItem(audio_ringbuf, item);

                xSemaphoreGive(stream_mutex);

                // Send chunk to client
                esp_err_t err = httpd_resp_send_chunk(req, (const char*)buffer, item_size);
                if (err != ESP_OK) {
                    ESP_LOGI(TAG, "Client disconnected, closing stream");
                    break;
                }
            } else {
                xSemaphoreGive(stream_mutex);
                // No data available yet, continue waiting
            }
        } else {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }

    stream_client_connected = false;
    if (buffer) {
        free(buffer);
    }

    // Send terminator - close connection
    httpd_resp_send_chunk(req, NULL, 0);

    return ESP_OK;
}

static void start_webserver(void) {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    httpd_handle_t server = NULL;
    if (httpd_start(&server, &config) == ESP_OK) {
        httpd_uri_t post = { .uri="/api/led", .method=HTTP_POST,    .handler=led_post_handler };
        httpd_uri_t get  = { .uri="/api/led", .method=HTTP_GET,     .handler=led_get_handler };
        httpd_uri_t opts = { .uri="/api/led", .method=HTTP_OPTIONS, .handler=cors_handler };
        httpd_uri_t status = { .uri="/api/status", .method=HTTP_GET,   .handler=status_handler };
        httpd_uri_t audio = { .uri="/api/audio/stream", .method=HTTP_GET, .handler=audio_stream_handler };

        httpd_register_uri_handler(server, &post);
        httpd_register_uri_handler(server, &get);
        httpd_register_uri_handler(server, &opts);
        httpd_register_uri_handler(server, &status);
        httpd_register_uri_handler(server, &audio);
        ESP_LOGI(TAG, "HTTP server started on port 80");
        ESP_LOGI(TAG, "Audio streaming endpoint: /api/audio/stream");
    }
}

// AFE handle and data globally available
static volatile bool afe_task_started = false;
static const esp_afe_sr_iface_t *afe_handle_global = NULL;
static esp_afe_sr_data_t *afe_data_global = NULL;

// Feed and detect tasks forward declaration
static void feed_task(void *arg);
static void detect_task(void *arg);

// Audio streaming function
static esp_err_t audio_stream_handler(httpd_req_t *req);

// ─── Wake Word with AFE from real microphone (runs on core 0 in its own task) ───
static void wake_word_task(void *arg) {
    ESP_LOGI(TAG, "Initializing audio board...");

    // Initialize the board with ES7210 ADC (mic input) and ES8311 codec
    ESP_ERROR_CHECK(esp_board_init(16000, 1, 16));  // 16kHz rate, 1 channel, 16 bit
    
    // Load models for wake word detection
    srmodel_list_t *models = esp_srmodel_init("model");
    if (models) {
        for (int i = 0; i < models->num; i++) {
            if (strstr(models->model_name[i], ESP_WN_PREFIX) != NULL) {
                ESP_LOGI(TAG, "Found wake word model: %s", models->model_name[i]);
            }
        }
    }
    
    char *model_name = esp_srmodel_filter(models, ESP_WN_PREFIX, "hilexin");
    if (!model_name) model_name = esp_srmodel_filter(models, ESP_WN_PREFIX, "hiesp");
    if (!model_name) { ESP_LOGE(TAG, "No wake word model! Available:");
        for (int i = 0; i < models->num; i++) {
            ESP_LOGE(TAG, "  %s", models->model_name[i]);
        }
        vTaskDelete(NULL); return; 
    }

    ESP_LOGI(TAG, "Initializing wakenet (this may take a while with PSRAM)...");
    esp_wn_iface_t *wakenet = (esp_wn_iface_t *)esp_wn_handle_from_name(model_name);
    model_iface_data_t *model_data = wakenet->create(model_name, DET_MODE_95);
    afe_data_global = (esp_afe_sr_data_t*)model_data;

    if (!afe_data_global) {
        ESP_LOGE(TAG, "Wake word model loading failed!");
        vTaskDelete(NULL);
        return;
    }

    afe_task_started = 1;

    ESP_LOGI(TAG, "Wake word model loaded, creating feed and detect tasks..." );

    // Create feed task for mic input and streaming
    xTaskCreatePinnedToCore(feed_task, "feed_task", 8192, (void*)afe_data_global, 5, NULL, 0);

    // Create detection task
    xTaskCreatePinnedToCore(detect_task, "detect_task", 8192, (void*)afe_data_global, 5, NULL, 1);

    vTaskDelay(portMAX_DELAY); // keep this task alive forever
}

// Feed task: reads from microphone and feeds to wake word detection and streaming buffer
static void feed_task(void *arg) {
    esp_afe_sr_data_t *afe_data = (esp_afe_sr_data_t *)arg;
    esp_wn_iface_t *wakenet = (esp_wn_iface_t *)esp_wn_handle_from_name(esp_srmodel_filter(esp_srmodel_init("model"), ESP_WN_PREFIX, "hilexin") ?: esp_srmodel_filter(esp_srmodel_init("model"), ESP_WN_PREFIX, "hiesp"));

    // Get the sample chunk size using the interface method
    int audio_chunksize = wakenet->get_samp_chunksize(afe_data);
    int16_t *buffer = (int16_t *)malloc(audio_chunksize * sizeof(int16_t));
    if (!buffer) {
        ESP_LOGE(TAG, "Failed to allocate buffer");
        return;
    }

    ESP_LOGI(TAG, "Feed task started - audio chunksize: %d", audio_chunksize);

    int feed_channel = esp_get_feed_channel();
    int16_t *temp_buffer = (int16_t *)malloc(audio_chunksize * sizeof(int16_t) * feed_channel);
    if (!temp_buffer) {
        ESP_LOGE(TAG, "Failed to allocate temp buffer");
        free(buffer);
        return;
    }

    while (afe_task_started) {
        // Get data from microphone
        esp_err_t result = esp_get_feed_data(true, temp_buffer, audio_chunksize * sizeof(int16_t) * feed_channel);
        if (result == ESP_OK) {
            // Run wakenet detection using the interface method
            wakenet_state_t state = wakenet->detect(afe_data, temp_buffer);
            if (state == WAKENET_DETECTED) {
                ESP_LOGI(TAG, "🔥 Wake word detected!");
                led_go(true, 4, 0, 100, 255, 500);  // blue breath 0.5s
            }

            // Add to ring buffer for streaming when connected
            if (stream_client_connected && audio_ringbuf) {
                if (xSemaphoreTake(stream_mutex, pdMS_TO_TICKS(10))) {
                    BaseType_t res = xRingbufferSend(audio_ringbuf, temp_buffer, audio_chunksize * sizeof(int16_t), pdMS_TO_TICKS(10));
                    if(res != pdTRUE) {
                        ESP_LOGD(TAG, "Audio streaming buffer full");
                    }
                    xSemaphoreGive(stream_mutex);
                }
            }
        } else {
            vTaskDelay(pdMS_TO_TICKS(10)); // Small delay on error
            continue;
        }
        vTaskDelay(pdMS_TO_TICKS(5)); // Small delay
    }

    if (buffer) free(buffer);
    if (temp_buffer) free(temp_buffer);
    vTaskDelete(NULL);
}

// Detection task: just keep things stable and potentially other processing
static void detect_task(void *arg) {
    model_iface_data_t *model_data = (model_iface_data_t *)arg;
    ESP_LOGI(TAG, "Detect task started");

    while (afe_task_started) {
        // Just keeping the task alive for stability
        // Wake word detection already happens in the feed task
        vTaskDelay(pdMS_TO_TICKS(100));
    }

    vTaskDelete(NULL);
}

// ─── Main ───
void app_main(void) {
    // Initialize audio ring buffer for streaming
    audio_ringbuf = xRingbufferCreate(AUDIO_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    if (!audio_ringbuf) {
        ESP_LOGE(TAG, "Failed to create audio ring buffer");
    } else {
        ESP_LOGI(TAG, "Audio ring buffer initialized (%d bytes)", AUDIO_RINGBUF_SIZE);
    }

    // Initialize semaphore for streaming mutex
    stream_mutex = xSemaphoreCreateMutex();
    if (!stream_mutex) {
        ESP_LOGE(TAG, "Failed to create streaming semaphore");
    }

    // Boot: blue spinner
    led_state.on = true;
    led_state.preset = 2;   // spinner
    led_state.r = 0;
    led_state.g = 100;
    led_state.b = 255;
    led_state.brightness = 200;
    led_state.auto_off_ms = 0;

    // LED task on core 1 - keeps INT WDT happy by yielding frequently
    xTaskCreatePinnedToCore(led_task, "led_task", 4096, NULL, 5, NULL, 1);
    ESP_LOGI(TAG, "Boot - blue spinner on");

    // Give LED task time to start
    vTaskDelay(pdMS_TO_TICKS(100));

    // Connect WiFi
    if (strlen(KORVO_WIFI_SSID) > 0) {
        wifi_init_sta();
        int waited = 0;
        while (!wifi_connected && waited < 30000) {
            vTaskDelay(pdMS_TO_TICKS(500));
            waited += 500;
        }
        if (wifi_connected) {
            ESP_LOGI(TAG, "WiFi ready!");
            vTaskDelay(pdMS_TO_TICKS(3500));
            start_webserver();
        } else {
            ESP_LOGW(TAG, "WiFi failed");
            led_go(true, 1, 255, 50, 0, 5000);  // solid red 5s then off
            vTaskDelay(pdMS_TO_TICKS(5500));
        }
    } else {
        ESP_LOGW(TAG, "No WiFi configured");
        led_go(false, 0, 0, 0, 0, 0);
    }

    // Wake word detection on core 0 - model loading happens here
    // The LED task on core 1 keeps running and feeding INT WDT
    wake_word_task(NULL);
}