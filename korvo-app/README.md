# Korvo Wake Word Agent — ESP-IDF Project

This is the standalone Korvo firmware project, separate from the esp-skainet examples.

## Architecture
- **main.c** — LED animations, WiFi, HTTP server, wake word detection
- **korvo_config.h** — Auto-generated WiFi credentials from web UI
- Single LED task owns all LED strip hardware access
- Wake word model loaded in a background task to avoid INT WDT crash

## Build & Flash
```bash
cd korvo-app
source ../setup.sh
idf.py set-target esp32
idf.py build
idf.py -p /dev/cu.usbserial-11210 flash
```

## Project Structure
- References esp-skainet components via EXTRA_COMPONENT_DIRS
- Uses managed components (led_strip, esp-sr) from the esp-skainet example