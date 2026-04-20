# Korvo Server

FastAPI replacement for the old Node `korvo-config-server`: WiFi DB, config generation, build/flash SSE, OpenRouter proxy, optional audio relay.

## Setup (virtualenv `korvo`)

From this directory (`korvo-server/`):

```bash
python3 -m venv korvo
source korvo/bin/activate
pip install -r requirements.txt
```

`./start.sh` also installs **pywhispercpp** from `requirements-whisper.txt` when it is missing (first run may compile; needs **CMake** and a C++ toolchain on some platforms). If that step fails, the server still starts; run `pip install -r requirements-whisper.txt` manually after fixing the toolchain.

Run:

```bash
source korvo/bin/activate
uvicorn korvo_server.main:app --host 0.0.0.0 --port 3333
```

Or: `python -m korvo_server.main` if you add a `__main__.py` — use `uvicorn` as above.

Open `http://localhost:3333/`.

## Audio relay

Same-origin low-latency playback from the dashboard can use:

`/api/audio/relay?url=http%3A%2F%2F<board-ip>%2Fapi%2Faudio%2Fstream`

Hosts are restricted to private LAN / `korvo.local` / localhost.

## Live transcription (Whisper.cpp → WebSocket)

Local transcription uses **[pywhispercpp](https://github.com/absadiki/pywhispercpp)** (Python bindings for [whisper.cpp](https://github.com/ggerganov/whisper.cpp)). After `pip install -r requirements-whisper.txt`, the dashboard **Start live transcript** control opens a WebSocket that:

1. Pulls **`http://<board>/api/audio/stream`** (same WAV / 16 kHz mono PCM as the relay).
2. Strips the 44-byte header and buffers PCM.
3. Every **`step_sec`** (default 1.25s), runs Whisper on the last **`window_sec`** (default 5s) of audio and sends JSON **`{ "type": "partial", "text": "…full window…", "delta": "…new words only…" }`**. **`delta`** removes words already present at the end of the previous window (word-aligned overlap), so the UI can append without repeating the overlapped span.

Endpoint (for custom clients):

`WS /ws/audio/transcribe?board_url=<url-encoded http://…/api/audio/stream>&model=base.en&step_sec=1.25&window_sec=5`

Models use short ids (`base.en`, `small`, …); weights are downloaded on first use (often `~/Library/Application Support/pywhispercpp/models/` on macOS, or `~/.local/share/pywhispercpp/models/` on Linux).

This is **chunked** transcription (sliding window), not whisper.cpp’s low-latency stream API, but it works with the Korvo infinite HTTP WAV stream without extra firmware.

## Record board audio to disk (VLC / monitoring)

The server can pull the board’s WAV stream once and write **`korvo-server/recordings/*.wav`**, then patch the RIFF/data sizes so **VLC** and **ffplay** accept the file. Files are also served at **`http://<host>:3333/recordings/<filename>.wav`**.

- **GET** (good for `curl`):

  `GET /api/audio/record?board_url=http://192.168.1.27/api/audio/stream&duration_sec=30`

  Optional: `&ffplay=1` to hear it while recording (requires **`ffplay`** from ffmpeg on the Mac).

- **POST** JSON: `{ "board_url": "...", "duration_sec": 30, "play_ffplay": false, "filename": null }`

Prefer the repo scripts (no `jq`):

```bash
chmod +x scripts/listen.sh scripts/record.sh   # once, if needed
./scripts/record.sh
KORVO_BOARD_IP=192.168.1.27 KORVO_RECORD_SEC=15 ./scripts/record.sh
./scripts/listen.sh
# or: bash scripts/listen.sh
# Diagnose silence (byte rate + curl -v + ffplay logs on stderr):
KORVO_PLAY_VERBOSE=1 ./scripts/listen.sh
```

Raw `curl` + `jq` is optional; if `jq` errors, the response is often HTML (wrong URL or server not running).

## WiFi / `korvo_config` got cleared?

The server DB lives at **`korvo-server/korvo.db`** (it is not the old `korvo-config-server/korvo.db`). If the DB is empty, we **no longer** overwrite `korvo_config.h` on every page load (`GET /api/config` is read-only). On first boot we still skip writing headers until you have an **active** WiFi row, unless you migrate an old DB.

If `korvo-config-server/korvo.db` still exists next to this repo, it is copied automatically **once** when `korvo-server/korvo.db` is missing.

Otherwise: add your network again in the UI (activate it), then **Build & Flash** so credentials return to the board. If you had committed good `korvo-app/main/korvo_config.h`, you can restore it from git before flashing.

## What moved

- UI template: `korvo_server/templates/pages/dashboard.html` (was `public/index.html`).
- WiFi scanner binaries: `korvo-server/bin/` (unchanged paths expected by `/api/networks`).
