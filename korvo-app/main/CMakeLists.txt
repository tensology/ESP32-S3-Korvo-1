idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_wifi
        esp_netif
        esp_event
        nvs_flash
        driver
        esp_http_server
        json
        esp_timer
        hardware_driver
        esp_codec_dev
        esp_psram
        mdns
        bt
)