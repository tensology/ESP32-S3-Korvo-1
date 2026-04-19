# Korvo Project State

## Board: ESP32-Korvo v1.1 (ESP32-D0WD-V3)
- UART bridge: CP2102N Silicon Labs
- Serial port: `/dev/cu.usbserial-11210`
- 12 WS2812 RGB LEDs on GPIO 33
- 3 analog microphones (not yet used in firmware)

## Project Structure (NEW — proper project folder)
```
Korvo/
├── korvo-app/                    # Our standalone ESP-IDF project
│   ├── main/
│   │   ├── main.c                # LED + WiFi + HTTP + wake word
│   │   ├── korvo_config.h        # Auto-generated WiFi credentials
│   │   ├── hilexin.h / hiesp.h  # Wake word model data (from esp-skainet build)
│   │   ├── CMakeLists.txt
│   │   └── idf_component.yml
│   ├── CMakeLists.txt            # Project root, EXTRA_COMPONENT_DIRS to esp-skainet
│   ├── partitions.csv            # nvs + phy_init + factory(1.5MB) + model(448KB)
│   ├── sdkconfig.defaults        # Flash 16MB QIO, WN models, INT WDT config
│   └── README.md
├── korvo-config-server/          # Web UI (Express + SQLite + SSE)
├── esp-skainet/                  # Upstream SDK (untouched, just referenced)
├── setup.sh
└── STATE.md
```

## What Works ✅
- ESP-IDF v5.4 building and flashing
- Board connects, boots, and runs without crashing
- **No more INT WDT crash!** Fixed by disabling CPU1 check in sdkconfig
- WS2812 LEDs: blue spinner on boot, green breathe on WiFi
- WiFi connection (intermittent — works on reboot)
- HTTP server on port 80 at /api/led
- Web UI at http://localhost:3333 with LED control
- Wake word model loads and detects (wn5_hilexin)
- Task watchdog triggers softly (warnings only, no crash)

## Current Issues ⚠️
- WiFi connection is intermittent (works sometimes, fails other times)
- Wake word fires false positives continuously (demo audio data, not real mic)
- WN5 model is deprecated — shows "Please switch to ESP32-S3 and wakenet9!"
- Task watchdog warning every 5s (main task doesn't yield to idle)
- Model partition not auto-flashed by idf.py (manual flash step needed)

## Key Configuration
- **INT WDT**: Enabled at 10s, CPU1 check DISABLED (prevents crash during PSRAM model load)
- **Flash**: 16MB QIO
- **LED**: 12 WS2812 on GPIO33, controlled by dedicated LED task
- **Wake word**: wn5_hilexin (deprecated; wn9s needs esp-skainet build system)

## Build & Flash
```bash
cd /Users/paul/development/TENSOLOGY/Korvo/korvo-app
source /Users/paul/development/TENSOLOGY/Korvo/setup.sh
idf.py set-target esp32   # first time only
idf.py build
# Flash firmware:
esptool.py --chip esp32 -p /dev/cu.usbserial-11210 erase_flash
python3 -m esptool --chip esp32 -p /dev/cu.usbserial-11210 -b 460800 \
  write_flash --flash_mode dio --flash_size 16MB --flash_freq 40m \
  0x1000 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0x10000 build/korvo-app.bin \
  0x190000 build/srmodels/srmodels.bin
```
Note: srmodels.bin must be copied from esp-skainet example build until
the build system generates it automatically.

## Serial Port
- Device: `/dev/cu.usbserial-11210`
- Chip: ESP32-D0WD-V3 (Rev 3 + PSRAM)
- UART chip: CP2102N

## WiFi (last known working)
- SSID: The Von
- ESP32 IP: 192.168.1.27 (when connected)

## Config Server
```bash
cd /Users/paul/development/TENSOLOGY/Korvo/korvo-config-server
node server.js &
# Open http://localhost:3333
```