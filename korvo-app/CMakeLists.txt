cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "0.2.0")

# Pull in esp-skainet hardware_driver for board init (needed by esp-sr)
# Skip perf_tester — it has API compatibility issues and we don't need it
set(EXTRA_COMPONENT_DIRS
    "${CMAKE_CURRENT_SOURCE_DIR}/../esp-skainet/components/hardware_driver"
    "${CMAKE_CURRENT_SOURCE_DIR}/../esp-skainet/components/player"
    "${CMAKE_CURRENT_SOURCE_DIR}/../esp-skainet/components/sr_ringbuf"
)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(korvo-app)