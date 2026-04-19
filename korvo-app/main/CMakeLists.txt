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
        esp_timer
        hardware_driver
        esp_codec_dev
        player
        esp_psram
        mdns
)

# Extra component dirs — pull in esp-skainet components and model data
set(EXTRA_COMPONENT_DIRS
    "${IDF_PATH}/../esp-skainet/components"
    "${IDF_PATH}/../esp-skainet/examples/wake_word_detection/wakenet/main"
    "${IDF_PATH}/../esp-skainet/components/hardware_driver"
)