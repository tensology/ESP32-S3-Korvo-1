# Korvo toolkit

<p align="center">
  <img src="readme/main.webp" alt="Korvo toolkit overview" />
</p>

This repository is a **development toolkit for the Espressif ESP32-Korvo v1.1** board: a voice-focused dev kit (three analogue mics, WS2812 strip, codec and amp, buttons). It is not a single “app binary” download—it is the pieces you use together to **run custom firmware**, **configure the board from a browser**, and **iterate without living only in a serial terminal**.

The **Korvo Config** web app is a single-page dashboard: a header (“Korvo Config”), a **tab bar** for each functional area, and one card (or panel) per concern. Templates live under [`korvo-server/korvo_server/templates/pages/dashboard/`](korvo-server/korvo_server/templates/pages/dashboard/): the shell is [`dashboard.html`](korvo-server/korvo_server/templates/pages/dashboard.html); each tab body is a partial in [`partials/`](korvo-server/korvo_server/templates/pages/dashboard/partials/) (e.g. `wifi.html`, `build.html`, `audio.html`). Static assets are under [`korvo-server/korvo_server/static/`](korvo-server/korvo_server/static/) (`css/dashboard/`, `js/dashboard/`).

## What you get

| Piece | Role |
|--------|------|
| **`korvo-app/`** | ESP-IDF firmware for Korvo v1.1: WiFi, HTTP APIs, wake word path, LED control, mic streaming hooks, and whatever product logic you add on the device. |
| **`korvo-server/`** | FastAPI **companion server** you run on your Mac: the **Korvo Config** dashboard (tabs below), **build & flash** with streamed logs, **audio** listen/push/inject, **Whisper** live transcript, **translation + TTS**, and SQLite-backed settings. |
| **`setup.sh`** (repo root) | Shell snippet to **source** so `idf.py` and the ESP-IDF toolchain are on your PATH when you work on firmware. Paths inside it are machine-specific—edit them once for your install. |
| **`start.sh`** (repo root) | Convenience wrapper: runs **`korvo-server/start.sh`**, which creates/updates the server venv and starts **Uvicorn on port 3333**. |

## Korvo Config dashboard — tab by tab

Tabs appear in this order (left to right). Use this as a map of the UI and the repo files that back each section.

### WiFi (`wifi.html`)

Scan for networks, enter SSID/password, **Save & Set Active**. Credentials are stored in the server database and used when generating config for the board. Good first stop after you can reach the dashboard.

### Build & Flash (`build.html`)

<p align="center">
  <img src="readme/build.webp" alt="Build and flash from the dashboard" />
</p>

Pick the **USB serial port** for your Korvo, optionally **Ninja clean** before build, then **Build & Flash to Board**. The server runs `idf.py` on **your Mac** and streams compiler output into the **terminal** (macOS-style window chrome, **Copy** for logs). Status is reflected on the main action button (building / flashing / done). This is the same workflow as a local terminal, but with one click and a live log pane. For tool diagnostics, the UI links to **`/api/flash/diag`**.

### Bluetooth (`bluetooth.html`)

**Bluetooth output**: scan, pick a speaker or headphones, **Connect**. Shows current and preferred device, optional **auto reconnect** on boot. The board acts as a client to your BT audio device—useful when you want playback off-device instead of the analogue jack.

### Settings (`settings.html`)

**Agent settings** persisted for the firmware story: **wake word**, HTTP **port** on the ESP32, **ESP32 IP / hostname** (for features that talk back to the board, e.g. LED control from the server), optional **agent endpoint**, default **LED brightness**, and **volume** hints. **Save Settings** writes into the server DB / config flow used by the app.

### LED (`led.html`)

The **LED** tab is the place to exercise the board’s **12× WS2812** ring (hardware-dependent; firmware must expose the HTTP endpoints the dashboard calls).

- **Power**: master on/off for the strip.
- **Presets**: motion patterns—**Solid**, **Spinner**, **Rainbow**, **Breathe**, **Pulse**, **Sparkle**, **Comet**, **Randomize**, and variants (e.g. reverse spin / rainbow).
- **Ring preview**: click segments to toggle pixels; use **Pick Colour** / **Apply all** for a custom RGB wash.
- **Solid colors**: quick swatches (red, green, blue, …)—tap once to light, again to turn off.
- **Brightness**: vertical slider (0–255).

Together this gives you **full colour control** and **animation presets** without recompiling—ideal for demos and for checking that the strip and firmware path are healthy.

### Audio (`audio.html`)

<p align="center">
  <img src="readme/stream.webp" alt="Audio stream / relay from the board" />
</p>

Three ideas in one tab: **listen**, **volume**, **push**.

1. **Listen to board microphone** — Enter the Korvo host (`korvo.local` or LAN IP). Optionally **play via this server** (same-origin relay, often smoother than the browser hitting the board directly). **Start live mic** decodes the infinite HTTP **WAV** stream with **Web Audio** (16 kHz mono), same spirit as `./scripts/listen.sh`.
2. **Playback volume** — One panel for **browser listen level**, gain on **push mic** / **file** PCM sent to the ESP, and (when CORS allows) **`POST` to the board’s output-volume API** so the device isn’t silent.
3. **Push audio to ESP32** — **Push mic** streams your machine’s microphone to the device over the server, or **upload a file** (FFmpeg transcodes to mono 16 kHz PCM). Only one of mic or file runs at a time. The server can **inject** PCM into the firmware playback path so you hear prompts or music on the board DAC/speaker path.

So: you can **hear** what the board hears, **tune** level end-to-end, and **stream** audio **to** the device for playback.

### ASR — Live transcript (`transcript.html`)

**Live transcript (Whisper.cpp)** on the **Mac**: the server pulls the same board mic stream, runs **pywhispercpp** on a sliding window, and sends **partials** and **deltas** over a WebSocket. Tune **model id**, **step seconds**, and **window seconds**; optional **listen** while transcribing. See [`korvo-server/README.md`](korvo-server/README.md) for the `WS /ws/audio/transcribe` URL shape.

### Translation (`translation.html`)

**Realtime translation**: type or paste text, choose source/target languages, optional **Speak target** (Kokoro or Polly → PCM → board inject). **Auto transcribing** ties the Whisper stream to **automatic translate + TTS** when a sentence is ready; settings for step/window live on the **ASR** tab. API keys for some flows live under **Third-Party**.

### Third-Party (`third_party.html`)

Store **vendor API keys** in SQLite (AssemblyAI, OpenAI, Anthropic, Gemini, AWS for Polly, etc.) with **Validate** actions where implemented. Keeps secrets out of the firmware and in one server-side store.

### Docs (`docs.html`)

**References & documentation**: links to Espressif **ESP32-Korvo** guides, **ESP-Skainet**, schematics PDFs, and an annotated hardware photo from upstream docs.

---

**Typical loop:** edit firmware in `korvo-app`, use **Build & Flash** and watch the terminal; use **WiFi** / **Settings** as needed; use **LED** to sanity-check the ring; use **Audio** / **ASR** / **Translation** when you want listen, transcribe, or speak without extra tools.

## Where to buy the board

The **ESP32-Korvo** dev kit (v1.1 class hardware this repo targets) shows up under Espressif’s **ESP32-KORVO** SKU at distributors and marketplaces, for example:

- [DigiKey (UK) — ESP32-KORVO](https://www.digikey.co.uk/en/products/detail/espressif-systems/ESP32-KORVO/12138980)
- [AliExpress listing](https://www.aliexpress.com/i/1005003980436945.html)

Stock and exact revision vary by seller; confirm you are getting the **ESP32** (not ESP32-S3) Korvo kit if you want to match this firmware’s `esp32` target.

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
├── readme/                   # Screenshots for this file (main, build, stream)
├── setup.sh                  # source ./setup.sh  → ESP-IDF env for firmware
├── start.sh                  # → korvo-server (dashboard on :3333)
├── korvo-app/                # ESP-IDF project (Korvo v1.1)
├── korvo-server/             # FastAPI dashboard + APIs
│   └── korvo_server/
│       ├── templates/pages/dashboard.html    # Dashboard shell
│       └── templates/pages/dashboard/partials/*.html  # One partial per tab
└── scripts/                  # Optional CLI helpers (listen, record, etc.)
```

## Documentation map

- **Product + firmware deep dive:** [`korvo-app/README.md`](korvo-app/README.md)
- **Server APIs, audio relay, Whisper WebSocket, translation/TTS, logs, DB paths:** [`korvo-server/README.md`](korvo-server/README.md)

Upstream hardware and speech stacks live in Espressif’s **[ESP-Skainet](https://github.com/espressif/esp-skainet)** and **[ESP-SR](https://github.com/espressif/esp-sr)** repos; this toolkit wires a concrete board + a local dev server around them.
