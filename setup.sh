#!/bin/bash
# ESP32-S3-Korvo-1 Development Environment Setup Script
# Source this file before building/flashing: source setup.sh
#
# This script sets up the ESP-IDF environment for the Korvo-1 project.
# It activates the correct Python virtual environment and adds all
# necessary tools to your PATH.

# ESP-IDF paths
export IDF_PATH="/Users/paul/esp/esp-idf"
export IDF_PYTHON_ENV_PATH="/Users/paul/.espressif/python_env/idf5.4_py3.12_env"

# Activate the ESP-IDF Python virtual environment
source "$IDF_PYTHON_ENV_PATH/bin/activate"

# Add ESP-IDF tools to PATH
export PATH="$IDF_PATH/tools:$PATH"
export PATH="/Users/paul/.espressif/tools/xtensa-esp-elf/esp-14.2.0_20260121/xtensa-esp-elf/bin:$PATH"
export PATH="/Users/paul/.espressif/tools/riscv32-esp-elf/esp-14.2.0_20260121/riscv32-esp-elf/bin:$PATH"
export PATH="/Users/paul/.espressif/tools/xtensa-esp-elf-gdb/16.3_20250913/xtensa-esp-elf-gdb/bin:$PATH"
export PATH="/Users/paul/.espressif/tools/riscv32-esp-elf-gdb/16.3_20250913/riscv32-esp-elf-gdb/bin:$PATH"
export PATH="/Users/paul/.espressif/tools/esp32ulp-elf/2.38_20240113/esp32ulp-elf/bin:$PATH"
export PATH="/Users/paul/.espressif/tools/openocd-esp32/v0.12.0-esp32-20260304/openocd-esp32/bin:$PATH"

# Ensure official ESP-IDF environment variables are exported (including ESP_ROM_ELF_DIR).
if [[ -f "$IDF_PATH/export.sh" ]]; then
  # shellcheck disable=SC1090
  source "$IDF_PATH/export.sh" >/dev/null 2>&1 || true
fi

# Fallback for setups where export.sh does not populate ESP_ROM_ELF_DIR.
if [[ -z "${ESP_ROM_ELF_DIR:-}" ]]; then
  _rom_tool_dir="$(ls -1d /Users/paul/.espressif/tools/esp-rom-elfs/* 2>/dev/null | sort | tail -n 1)"
  if [[ -n "$_rom_tool_dir" && -d "$_rom_tool_dir" ]]; then
    export ESP_ROM_ELF_DIR="${_rom_tool_dir}/"
  fi
  unset _rom_tool_dir
fi

echo "✓ ESP-IDF v5.4 environment activated (ESP32-S3 Korvo-1)"
echo "✓ IDF_PATH: $IDF_PATH"
echo ""
echo "Available commands:"
echo "  idf.py build          - Build the project"
echo "  idf.py flash          - Flash firmware to the board"
echo "  idf.py monitor        - Open serial monitor"
echo "  idf.py flash monitor  - Flash and monitor"
echo "  idf.py menuconfig     - Configure project options"
echo ""
echo "Typical flash command:"
echo "  idf.py -p /dev/cu.usbserial-* flash monitor"