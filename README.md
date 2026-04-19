# ESP32-S3-Korvo-1 Project

Development environment for the **ESP32-S3-Korvo-1 v5.0** AI voice development board.

## Board Overview

The ESP32-S3-Korvo-1 is an AI development board by Espressif based on the ESP32-S3 chip with:
- **ESP32-S3-WROOM-1** module (8MB PSRAM + 16MB Flash)
- 3-microphone array (analog) for far-field voice pickup
- 12 addressable RGB LEDs (WS2812C)
- Speaker output, headphone output
- Audio ADC (ES7210), Audio Codec (ES8311)
- Class D audio amplifier (3W)
- Boot, Reset, and 6 function buttons
- USB Power Port + USB-to-UART Port (both Micro-USB)
- MicroSD card slot

## Quick Start

### 1. Source the environment

```bash
cd /Users/paul/development/TENSOLOGY/Korvo
source setup.sh
```

### 2. Connect the board

- Connect **USB Power Port** to a USB power source
- Connect **USB-to-UART Port** to your Mac (this is the programming/debug port)
- Toggle the **Power Switch** to ON
- Connect a speaker (4Ω 3W recommended) or headphones

### 3. Find the serial port

```bash
ls /dev/cu.usbserial-* 
# Typically: /dev/cu.usbserial-0001 or similar
```

If no port appears, you may need to install the **SiLabs CP210x driver** for the USB-to-UART bridge:
- Download from: https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers
- On macOS, the driver may need to be allowed in System Settings > Privacy & Security

### 4. Build and flash an example

```bash
# English speech command recognition (recommended starting point)
cd esp-skainet/examples/en_speech_commands_recognition
idf.py -p /dev/cu.usbserial-* flash monitor

# Or wake word detection
cd esp-skainet/examples/wake_word_detection/wakenet
idf.py -p /dev/cu.usbserial-* flash monitor
```

### 5. Configure for Korvo-1 board

```bash
idf.py menuconfig
# Navigate to: Audio Board → Select ESP32-S3-Korvo-1
```

## Available Examples (in esp-skainet/examples/)

| Example | Description |
|---------|-------------|
| `en_speech_commands_recognition` | English speech command recognition |
| `cn_speech_commands_recognition` | Chinese speech command recognition |
| `wake_word_detection/wakenet` | Wake word detection (Hi ESP, etc.) |
| `wake_word_detection/afe` | Wake word with Audio Front-End (2-mic) |
| `chinese_tts` | Chinese text-to-speech |
| `voice_communication` | Voice communication example |
| `deep_noise_suppression` | Deep noise suppression demo |
| `direction_of_arrival` | Direction of arrival detection |
| `voice_activity_detection` | Voice activity detection |

## Repository Structure

```
Korvo/
├── setup.sh                    # Environment setup script
├── README.md                   # This file
├── esp-skainet/                # ESP-Skainet: Voice assistant SDK
│   ├── components/             # Hardware driver, player, etc.
│   ├── examples/               # Example applications
│   └── tools/                  # Default firmware binaries
└── esp-idf/ → ~/esp/esp-idf    # ESP-IDF v5.4 (referenced via IDF_PATH)
```

## ESP-IDF Version

Using **ESP-IDF v5.4** (release/v5.4 branch) with Python 3.12 virtual environment.

## Key Resources

- [ESP-Skainet GitHub](https://github.com/espressif/esp-skainet)
- [ESP-SR (speech recognition) GitHub](https://github.com/espressif/esp-sr)
- [ESP-IDF Documentation](https://docs.espressif.com/projects/esp-idf/en/release-v5.4/esp32s3/)
- [ESP32-S3-Korvo-1 User Guide](https://github.com/espressif/esp-skainet/blob/master/docs/en/hw-reference/esp32s3/user-guide-korvo-1.md)
- [ESP-SR Documentation](https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/)
- [ESP32-S3 Datasheet](https://www.espressif.com/sites/default/files/documentation/esp32-s3_datasheet_en.pdf)

## Wake Words Available

| Wake Word | Model ID |
|-----------|----------|
| Hi, ESP | `wn9_hiesp` |
| Hi, 乐鑫 | `wn9_hilexin` |
| Hi, Jason | `wn9_hijason_tts2` |
| Alexa | `wn9_alexa` |
| Jarvis | `wn9_jarvis_tts` |
| Computer | `wn9_computer_tts` |

## Entering Download Mode

If `idf.py flash` doesn't work automatically:
1. Hold down the **Boot** button
2. Press and release the **Reset** button
3. Release the **Boot** button
4. The board is now in Firmware Download mode

## Serial Monitor

Press `Ctrl+]` to exit the serial monitor.