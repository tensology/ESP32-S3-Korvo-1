cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "0.2.0")

# Shadow esp-skainet board header before any component compiles (MCLK off GPIO0).
include_directories(BEFORE "${CMAKE_CURRENT_LIST_DIR}/korvo_board_override")

# Pull in esp-skainet hardware_driver for board init.
set(EXTRA_COMPONENT_DIRS
    "${CMAKE_CURRENT_SOURCE_DIR}/../esp-skainet/components/hardware_driver"
)
set(EXCLUDE_COMPONENTS esp_gdbstub)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(korvo-app)

# hardware_driver adds boards/include before this file's include_directories; force our header first.
if(TARGET __idf_hardware_driver)
    target_include_directories(__idf_hardware_driver BEFORE PRIVATE "${CMAKE_CURRENT_LIST_DIR}/korvo_board_override")
endif()