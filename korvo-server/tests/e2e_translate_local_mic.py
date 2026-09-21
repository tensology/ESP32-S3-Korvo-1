"""
Local end-to-end harness for the Korvo translation pipeline WITHOUT a physical
ESP32 board.

How it works
------------
The real auto-translate flow is:

    board mic  ->  HTTP WAV stream (board_url)
               ->  audio_hub.subscribe(board_url)
               ->  /ws/audio/transcribe  (Whisper)
               ->  sentence JSON
               ->  /api/translate/google  (Google + Kokoro/Polly TTS)

The board is ONLY a source of 16 kHz mono WAV audio at the front of that chain.
This harness substitutes the board with a *local microphone* (or a recorded WAV
file) by running a tiny HTTP server that streams WAV into the same audio_hub the
server already uses. Everything downstream — Whisper ASR, translation, TTS —
is the real production code path.

The substitution point is the single module-level variable ``AUDIO_SOURCE``.

    AUDIO_SOURCE = "mic"       -> capture from the local macOS microphone (default)
    AUDIO_SOURCE = "wav_file"  -> replay a recorded WAV (repeatable, no speaking)
    AUDIO_SOURCE = "/path.wav" -> same as wav_file when it looks like a path
    AUDIO_SOURCE = "board_url"-> would point at a real device (not used here)

Requirements (runtime, not imported at import time):
    - korvo-server running (FastAPI app) on BASE_URL
    - pywhispercpp installed (Whisper backend) for the ASR stage
    - Kokoro/afplay for server_local TTS (macOS)
    - ffmpeg on PATH for the "mic" source (avfoundation)

Run:
    python3 tests/e2e_translate_local_mic.py

It prints per-stage status and exits non-zero on the first broken stage.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# CONFIGURATION  (this is the substitution point)                             #
# --------------------------------------------------------------------------- #
# Swap this single variable to change where audio "enters" the pipeline.
#   "mic"        -> local microphone via ffmpeg/avfoundation
#   "wav_file"   -> a recorded WAV file (set WAV_FILE_PATH)
#   "/abs/x.wav" -> a recorded WAV file given as a path
#   "board_url"  -> real device (set BOARD_MIC_URL); not used in local testing
AUDIO_SOURCE: str = os.environ.get("KORVO_AUDIO_SOURCE", "mic")

# Where the korvo-server is listening.
BASE_URL: str = os.environ.get("KORVO_BASE_URL", "http://127.0.0.1:3333").rstrip("/")

# Local WAV file used when AUDIO_SOURCE == "wav_file" or a path.
WAV_FILE_PATH: str = os.environ.get("KORVO_WAV_FILE", "tests/sample_phrase.wav")

# Used only when AUDIO_SOURCE == "board_url".
BOARD_MIC_URL: str = os.environ.get("KORVO_BOARD_MIC_URL", "")

# Translation direction for the downstream /api/translate/google call.
SOURCE_LANG: str = os.environ.get("KORVO_SOURCE_LANG", "en")
TARGET_LANG: str = os.environ.get("KORVO_TARGET_LANG", "ja")

# Whisper model id (must match what the server loaded).
WHISPER_MODEL: str = os.environ.get("KORVO_WHISPER_MODEL", "base.en")

# How long (seconds) to listen for a sentence before giving up.
LISTEN_TIMEOUT_SEC: float = float(os.environ.get("KORVO_LISTEN_TIMEOUT", "25"))

# avfoundation device index for the mac microphone ("0" = first audio device).
MIC_DEVICE: str = os.environ.get("KORVO_MIC_DEVICE", "1")

# Fake "board" URL the harness publishes mic audio under. The server's
# audio_hub treats this exactly like a real board stream URL.
FAKE_BOARD_URL: str = "http://127.0.0.1:18123/mic"


@dataclass
class StageResult:
    name: str
    ok: bool
    detail: str


# --------------------------------------------------------------------------- #
# Local WAV-streaming HTTP server (the "fake board")                          #
# --------------------------------------------------------------------------- #
class _MicWavServer:
    """Streams 16 kHz mono WAV to any HTTP GET client, like a board would.

    Yields a 44-byte RIFF header once, then raw PCM. The server's
    WavStreamToPcm16 parser strips the first 44 bytes and passes through PCM,
    so this matches the board's wire format exactly.
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._proc: subprocess.Popen | None = None
        self._wav_file: str | None = None
        self._loop = asyncio.get_event_loop()

    def _wav_header(self, nchannels: int = 1, sampwidth: int = 2, framerate: int = 16000) -> bytes:
        import struct
        hdr = b"RIFF"
        hdr += struct.pack("<I", 0xFFFFFFFF)  # size unknown (streaming)
        hdr += b"WAVEfmt "
        hdr += struct.pack("<IHHIIHH", 16, 1, nchannels, framerate,
                           framerate * nchannels * sampwidth,
                           nchannels * sampwidth, sampwidth * 8)
        hdr += b"data"
        hdr += struct.pack("<I", 0xFFFFFFFF)
        return hdr

    def start(self) -> None:
        if self._source == "board_url":
            # Real device: the server streams from BOARD_MIC_URL directly.
            return
        if self._source in ("wav_file",) or self._source.lower().endswith(".wav"):
            path = WAV_FILE_PATH if self._source == "wav_file" else self._source
            if not os.path.exists(path):
                raise FileNotFoundError(f"WAV file not found: {path}")
            self._wav_file = path
            return
        if self._source == "mic":
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg not found on PATH (required for mic source)")
            # ffmpeg captures the mac mic and emits a streaming WAV on stdout.
            self._proc = subprocess.Popen(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "avfoundation", "-i", f":{MIC_DEVICE}",
                    "-ar", "16000", "-ac", "1", "-f", "wav", "-",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            return
        raise ValueError(f"Unknown AUDIO_SOURCE: {self._source!r}")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
            self._proc = None

    async def stream_to(self, writer) -> None:
        """Copy WAV bytes to the HTTP response writer until EOF."""
        if self._source == "board_url":
            return  # server handles upstream directly
        if self._wav_file:
            with open(self._wav_file, "rb") as f:
                while True:
                    chunk = f.read(16384)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
                    await asyncio.sleep(0.01)
            return
        # mic: stdout of ffmpeg is already a WAV stream (header + PCM).
        # The server's audio_hub may open a second connection after the first
        # sentence finalizes; the original ffmpeg pipe has already ended by then,
        # so just exit cleanly instead of erroring.
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                chunk = await self._loop.run_in_executor(None, proc.stdout.read, 16384)
            except Exception:
                break
            if not chunk:
                break
            try:
                writer.write(chunk)
                await writer.drain()
            except Exception:
                break


async def _start_mic_server() -> object:
    from urllib.parse import urlparse

    server = _MicWavServer(AUDIO_SOURCE)
    server.start()
    port = urlparse(FAKE_BOARD_URL).port or 18123

    async def _handle(reader, writer) -> None:
        resp_headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: audio/wav\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(resp_headers.encode())
        try:
            await server.stream_to(writer)
        except Exception as e:  # noqa: BLE001
            print(f"[mic-server] stream error: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    srv = await asyncio.start_server(_handle, "127.0.0.1", port)
    print(f"[mic-server] streaming AUDIO_SOURCE={AUDIO_SOURCE!r} on {FAKE_BOARD_URL}")
    return srv, server


# --------------------------------------------------------------------------- #
# Pipeline stages                                                             #
# --------------------------------------------------------------------------- #
def _http_json(method: str, path: str, body: dict | None = None, timeout: float = 90) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


async def _connect_transcribe_ws() -> tuple["WebSocketClientProtocol", list[dict]]:
    import websockets

    proto = "ws" if BASE_URL.startswith("http://") else "wss"
    host = BASE_URL.split("://", 1)[1]
    q = urllib.parse.urlencode({
        "board_url": FAKE_BOARD_URL if AUDIO_SOURCE != "board_url" else BOARD_MIC_URL,
        "model": WHISPER_MODEL,
        "step_sec": "1.0",
        "window_sec": "4.0",
    })
    ws_url = f"{proto}://{host}/ws/audio/transcribe?{q}"
    ws = await websockets.connect(ws_url, open_timeout=10, close_timeout=5)
    return ws, []


async def run_pipeline() -> list[StageResult]:
    results: list[StageResult] = []

    # --- Stage 0: server reachable ---
    try:
        _http_json("GET", "/api/translation/tts-sessions")
        results.append(StageResult("server_reachable", True, BASE_URL))
    except Exception as e:  # noqa: BLE001
        results.append(StageResult("server_reachable", False, str(e)))
        return results

    # --- Stage 1: Whisper/ASR via the real WS endpoint, fed by local mic ---
    sentences: list[str] = []
    try:
        ws, _ = await _connect_transcribe_ws()
        try:
            # Wait for 'ready' then collect sentences until timeout/listen window.
            deadline = time.monotonic() + LISTEN_TIMEOUT_SEC
            ready = False
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.5))
                except asyncio.TimeoutError:
                    continue
                if msg.get("type") == "ready":
                    ready = True
                    print(f"[asr] websocket ready (model={msg.get('model')})")
                elif msg.get("type") == "error":
                    raise RuntimeError(f"WS error: {msg.get('message')}")
                elif msg.get("type") == "sentence":
                    text = (msg.get("text") or "").strip()
                    if text:
                        print(f"[asr] sentence: {text!r}")
                        sentences.append(text)
                        break  # one sentence is enough to prove the chain
            if not ready:
                results.append(StageResult("asr_whisper", False, "never received 'ready' from WS"))
            elif not sentences:
                results.append(StageResult("asr_whisper", False, "no sentence captured in listen window"))
            else:
                results.append(StageResult("asr_whisper", True, f"captured: {sentences[0]!r}"))
        finally:
            await ws.close()
    except Exception as e:  # noqa: BLE001
        results.append(StageResult("asr_whisper", False, str(e)))
        return results

    # --- Stage 2: translation + TTS (server_local so no device needed) ---
    if not sentences:
        results.append(StageResult("translate_tts", False, "skipped: no sentence from ASR stage"))
        return results
    phrase = sentences[0]
    try:
        resp = _http_json("POST", "/api/translate/google", {
            "text": phrase,
            "source_language": SOURCE_LANG,
            "target_language": TARGET_LANG,
            "speak_target": True,
            "playback_target": "server_local",
            "kokoro_voice": "af_heart",
            "kokoro_speed": 1.0,
            "tts_sentence_stream": True,
        }, timeout=90)
        translated = (resp.get("translated_text") or "").strip()
        if not translated:
            results.append(StageResult("translate_tts", False, f"empty translation; resp={resp}"))
        else:
            results.append(StageResult(
                "translate_tts", True,
                f"{SOURCE_LANG}->{TARGET_LANG}: {translated!r} "
                f"(vendor={resp.get('tts_vendor_used')}, pending={resp.get('speak_pending')})",
            ))
    except Exception as e:  # noqa: BLE001
        results.append(StageResult("translate_tts", False, str(e)))

    return results


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #
async def main() -> int:
    print("=" * 64)
    print("Korvo translation E2E (no device) — AUDIO_SOURCE =", repr(AUDIO_SOURCE))
    print("=" * 64)
    srv = None
    mic_server = None
    try:
        if AUDIO_SOURCE != "board_url":
            srv, mic_server = await _start_mic_server()
        results = await run_pipeline()
    finally:
        if mic_server is not None:
            mic_server.stop()
        if srv is not None:
            srv.close()
            await srv.wait_closed()

    print("\n--- RESULTS ---")
    failed = 0
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        if not r.ok:
            failed += 1
        print(f"[{mark}] {r.name}: {r.detail}")
    print("---------------")
    if failed:
        print(f"{failed} stage(s) failed.")
        return 1
    print("All stages passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
