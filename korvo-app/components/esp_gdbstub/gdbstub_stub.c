#include "esp_gdbstub.h"

/*
 * Local override for ESP-IDF esp_gdbstub component.
 * This project does not use gdbstub at runtime, and the upstream
 * xtensa implementation currently fails to compile in this toolchain.
 */
