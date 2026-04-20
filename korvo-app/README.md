# Korvo firmware (`korvo-app`)

ESP-IDF application targeting **ESP32-Korvo v1.1** (ESP32-WROVER-class module, three analogue mics, 12× WS2812, ES7210 / ES8311 audio path). This folder is the on-device half of the repo; the web dashboard and build host live in **`korvo-server/`**.

## Architecture

- **`main/main.c`** — LED behaviour, WiFi, HTTP server, wake word pipeline integration, and related product logic.
- **`main/korvo_config.h`** — WiFi (and similar) credentials; the companion server can regenerate this from the dashboard after you save networks.
- **Single LED task** owns the WS2812 strip to avoid races.
- **Wake word** model load runs in a background task to reduce interrupt watchdog pressure.
- **`korvo_board_override/`** — Board-specific headers when you need to diverge from stock Skainet board files.

Components pull in **ESP-Skainet** pieces via `EXTRA_COMPONENT_DIRS` in `main/CMakeLists.txt` (paths under `IDF_PATH/../esp-skainet/...`). You need a clone of **[esp-skainet](https://github.com/espressif/esp-skainet)** next to your **`esp-idf`** tree (sibling of `IDF_PATH`), matching the layout those CMake variables expect.

## Prerequisites

- **ESP-IDF** (this repo’s root `setup.sh` is written for a v5.4-style tree; adjust `IDF_PATH` and tool paths inside **`../setup.sh`** for your machine).
- **Python** venv for IDF as usual for Espressif installs.
- **USB serial** to the Korvo **UART** port for flash and monitor.

## Environment (every new terminal)

From **`korvo-app/`**:

```bash
source ../setup.sh
```

If `idf.py` is not found, fix paths in `setup.sh` at the repo root.

## Serial port

List macOS device nodes:

```bash
ls /dev/cu.usbserial-* /dev/cu.SLAB_USBtoUART* 2>/dev/null
```

If nothing appears, install the **Silicon Labs CP210x** (or your bridge chip) VCP driver and approve it under **System Settings → Privacy & Security** on macOS.

## Build and flash

```bash
cd korvo-app
source ../setup.sh
idf.py set-target esp32
idf.py build
idf.py -p /dev/cu.usbserial-XXXXXXXX flash monitor
```

Replace the port with your actual device. You can also drive **clean rebuild + flash** from the **`korvo-server`** dashboard (Build tab) if the server can reach your toolchain and the serial port.

### `menuconfig` / board selection

When working inside upstream **esp-skainet examples**, use **menuconfig** to pick the Korvo board variant. This **`korvo-app`** project already targets the v1.1-style Korvo through its CMake `EXTRA_COMPONENT_DIRS` and board overrides; treat `menuconfig` as needed when you add components or change partition layouts.

### Download (bootloader) mode

If auto-reset flash fails:

1. Hold **Boot**.
2. Tap **Reset**.
3. Release **Boot**.
4. Run `idf.py -p … flash` again.

## Serial monitor

In `idf.py monitor`, exit with **Ctrl+]** (default ESP-IDF binding).

## Optional: upstream Skainet examples

If you also want the stock demos (speech commands, other wake pipelines, etc.), build from inside a cloned **`esp-skainet/examples/...`** tree with the same sourced IDF environment, and in **menuconfig** set **Audio board → ESP32-Korvo v1.1** (or equivalent) where the example supports it.

| Path (under `esp-skainet/examples/`) | Notes |
|----------------------------------------|--------|
| `en_speech_commands_recognition` | English command recognition |
| `cn_speech_commands_recognition` | Chinese command recognition |
| `wake_word_detection/wakenet` | Wake word only |
| `wake_word_detection/afe` | Wake word + AFE |
| `chinese_tts` | Chinese TTS |
| `voice_communication` | Voice comm demo |
| `deep_noise_suppression` | DNS demo |
| `direction_of_arrival` | DoA |
| `voice_activity_detection` | VAD |

## Wake word model IDs (ESP-SR / wakenet)

| Wake word | Model ID |
|-----------|----------|
| Hi, ESP | `wn9_hiesp` |
| Hi, 乐鑫 | `wn9_hilexin` |
| Hi, Jason | `wn9_hijason_tts2` |
| Alexa | `wn9_alexa` |
| Jarvis | `wn9_jarvis_tts` |
| Computer | `wn9_computer_tts` |

## Hardware reference (v1.1)

Rough feature set of **ESP32-Korvo v1.1** (not exhaustive):

- ESP32 module with PSRAM (WROVER-class configurations common on this kit).
- Three analogue microphones for capture / beamforming-style use in SR stacks.
- 12 addressable RGB LEDs (WS2812-class).
- ES7210 ADC, ES8311 codec, speaker and headphone paths, onboard amplifier.
- Boot, reset, and function buttons; UART/USB for flashing; SD slot on the PCB.

Where to buy (ESP32-KORVO kit; confirm revision with the seller): [DigiKey UK](https://www.digikey.co.uk/en/products/detail/espressif-systems/ESP32-KORVO/12138980), [AliExpress](https://www.aliexpress.com/i/1005003980436945.html).

Official docs and PDFs:

- [ESP32-Korvo v1.1 user guide (ESP-Skainet docs)](https://github.com/espressif/esp-skainet/blob/master/docs/en/hw-reference/esp32/user-guide-esp32-korvo-v1.1.md)
- [ESP-Skainet (GitHub)](https://github.com/espressif/esp-skainet)
- [ESP-SR](https://github.com/espressif/esp-sr) — [ESP-SR documentation](https://docs.espressif.com/projects/esp-sr/en/latest/esp32/)
- [ESP-IDF (ESP32)](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/index.html)
- [ESP32 series datasheet](https://www.espressif.com/sites/default/files/documentation/esp32_datasheet_en.pdf)

## Project layout (this folder)

```
korvo-app/
├── CMakeLists.txt
├── sdkconfig.defaults
├── partitions.csv
├── main/
│   ├── CMakeLists.txt        # EXTRA_COMPONENT_DIRS → esp-skainet
│   ├── main.c
│   └── korvo_config.h        # generated / edited for WiFi, etc.
├── korvo_board_override/     # BSP overrides for v1.1
└── README.md                 # This file
```
