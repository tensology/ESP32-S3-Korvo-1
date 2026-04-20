# Korvo toolkit

This repository is a **development toolkit for the Espressif ESP32-Korvo v1.1** board: a voice-focused dev kit (three analogue mics, WS2812 strip, codec and amp, buttons). It is not a single “app binary” download—it is the pieces you use together to **run custom firmware**, **configure the board from a browser**, and **iterate without living only in a serial terminal**.

## What you get

| Piece | Role |
|--------|------|
| **`korvo-app/`** | ESP-IDF firmware for Korvo v1.1: WiFi, HTTP APIs, wake word path, LED control, mic streaming hooks, and whatever product logic you add on the device. |
| **`korvo-server/`** | FastAPI **companion server** you run on your Mac: web dashboard (WiFi, settings, LED playground, **build and flash** with streamed logs), optional **audio relay** and **live transcription** (Whisper) from the board’s HTTP mic stream, and small utilities (recording, WiFi scan helpers). |
| **`setup.sh`** (repo root) | Shell snippet to **source** so `idf.py` and the ESP-IDF toolchain are on your PATH when you work on firmware. Paths inside it are machine-specific—edit them once for your install. |
| **`start.sh`** (repo root) | Convenience wrapper: runs **`korvo-server/start.sh`**, which creates/updates the server venv and starts **Uvicorn on port 3333**. |

Typical loop: edit firmware in `korvo-app`, use the dashboard to **flash** and watch **SSE logs**, use **Audio** / **ASR** tabs when you want to hear or transcribe the mic without extra wiring. The server talks to the board over your LAN (or `korvo.local` when mDNS behaves).

## Quick start (server)

From the repo root:

```bash
./start.sh
```

Open **http://localhost:3333/**. First run may install Python dependencies (and optionally Whisper-related wheels); details and endpoints are in [`korvo-server/README.md`](korvo-server/README.md).

## Quick start (firmware)

Firmware build instructions, `esp-skainet` layout expectations, serial port notes, flash/monitor, and download-mode steps are in **[`korvo-app/README.md`](korvo-app/README.md)**. Short version:

```bash
cd korvo-app
source ../setup.sh
idf.py build
idf.py -p /dev/cu.usbserial-XXXXXXXX flash monitor
```

## Repository layout

```
Korvo/
├── README.md                 # This overview
├── setup.sh                  # source ./setup.sh  → ESP-IDF env for firmware
├── start.sh                  # → korvo-server (dashboard on :3333)
├── korvo-app/                # ESP-IDF project (Korvo v1.1)
├── korvo-server/             # FastAPI dashboard + APIs
├── scripts/                  # Optional CLI helpers (listen, record, etc.)
└── korvo-config-server/      # Legacy Node config server (superseded by korvo-server)
```

## Documentation map

- **Product + firmware deep dive:** [`korvo-app/README.md`](korvo-app/README.md)
- **Server APIs, audio relay, Whisper WebSocket, DB paths:** [`korvo-server/README.md`](korvo-server/README.md)

Upstream hardware and speech stacks live in Espressif’s **[ESP-Skainet](https://github.com/espressif/esp-skainet)** and **[ESP-SR](https://github.com/espressif/esp-sr)** repos; this toolkit wires a concrete board + a local dev server around them.
