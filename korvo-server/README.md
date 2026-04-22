<h1 align="center">Korvo Server</h1>

<p align="center">
  The local web dashboard that powers Korvo setup, build/flash, audio testing, live transcription, and translation.
</p>

<p align="center">
  <a href="#quick-start"><strong>Quick Start</strong></a> ·
  <a href="#what-you-get"><strong>What You Get</strong></a> ·
  <a href="#daily-workflow"><strong>Daily Workflow</strong></a> ·
  <a href="#troubleshooting"><strong>Troubleshooting</strong></a>
</p>

---

## Quick start

From `korvo-server/`:

```bash
./start.sh
```

Then open:

`http://localhost:3333/`

That is enough for most users. The startup script creates a local Python environment, installs what is needed, and launches the dashboard server.

---

## What you get

| | Feature | Why it matters |
|---|---|---|
| 📶 | **WiFi setup** | Save and manage board network credentials from the browser |
| 🧱 | **Build & Flash** | Build and flash firmware with one click and live output |
| 🎧 | **Bluetooth controls** | Connect playback devices and manage preferred output |
| 🌈 | **LED controls** | Test colours, presets, and brightness on the board ring |
| 🎙️ | **Audio tools** | Listen to board mic and push audio from your machine to the board |
| 📝 | **Live transcript** | See speech-to-text updates while audio is streaming |
| 🌍 | **Translation + speech** | Translate text and optionally play spoken responses |
| 🔑 | **Third-party keys** | Save API keys used by optional integrations |
| 💾 | **Local persistence** | Settings and keys are stored in the local SQLite database |

For a complete visual tour of every dashboard tab, use the root guide: [`../README.md`](../README.md).

---

## Daily workflow

Most sessions follow this sequence:

1. Start the server with `./start.sh`
2. Open `http://localhost:3333/`
3. Plug in your board and use **Build & Flash**
4. Confirm **WiFi** and **Settings**
5. Test **LED** and **Audio**
6. Use **ASR** and **Translation** if needed

---

## Optional manual start

If you want to run things manually instead of using `./start.sh`:

```bash
python3 -m venv korvo
source korvo/bin/activate
pip install -r requirements.txt
uvicorn korvo_server.main:app --host 0.0.0.0 --port 3333
```

Notes:

- Some speech features may install extra dependencies on first run.
- If optional whisper components fail to build, the dashboard still starts; install optional packages later when your toolchain is ready.

---

## Where things are stored

- Database: `korvo-server/korvo.db`
- Logs: `korvo-server/logs/`
- Audio recordings: `korvo-server/recordings/`

---

## Troubleshooting

### Dashboard does not open

- Confirm the server is running from `korvo-server/`
- Check for startup errors in terminal output
- Retry `./start.sh`

### Build & flash cannot find your board

- Reconnect USB cable
- Choose the correct serial port in the dashboard
- Try closing apps that may already be using the serial port

### No audio during listen/push tests

- Confirm board IP/host is correct
- Verify board and server are on the same network
- Check volume controls in the Audio tab

### WiFi entries seem missing

- The active database is `korvo-server/korvo.db`
- Re-add WiFi in the dashboard and set it active, then build/flash again

---

## Technical reference

Use these when you need implementation details:

- Full product walkthrough and screenshots: [`../README.md`](../README.md)
- Firmware-specific guide: [`../korvo-app/README.md`](../korvo-app/README.md)
- Dashboard shell: `korvo_server/templates/pages/dashboard.html`
- Dashboard tab partials: `korvo_server/templates/pages/dashboard/partials/`
- Static assets: `korvo_server/static/`

---

## Advanced notes

If you are building custom integrations, there are dedicated server routes for:

- audio relay
- live transcription websocket
- translation and TTS
- recording streams to file

Keep this README focused on setup and usage; API-level behavior can stay in code docs and module comments.
