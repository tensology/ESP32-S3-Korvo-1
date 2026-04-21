from __future__ import annotations

import array
import asyncio
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx


def normalize_board_base(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, flags=re.I):
        value = f"http://{value}"
    return value.rstrip("/")


def clamp_stream_volume(linear: float) -> float:
    """Linear gain for mono s16le PCM (0 = silence, 1 = unity)."""
    try:
        v = float(linear)
    except (TypeError, ValueError):
        return 1.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def scale_s16le_mono_pcm(chunk: bytes, linear: float) -> bytes:
    """Apply linear gain to little-endian mono int16 PCM (Korvo inject format)."""
    g = clamp_stream_volume(linear)
    if g >= 0.9999 or not chunk:
        return chunk
    if len(chunk) < 2:
        return chunk
    n = len(chunk) // 2
    a = array.array("h")
    a.frombytes(chunk[: n * 2])
    for i in range(len(a)):
        v = int(round(float(a[i]) * g))
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        a[i] = v
    return a.tobytes()


async def _post_inject_chunk_with_backpressure(
    client: httpx.AsyncClient,
    inject_url: str,
    chunk: bytes,
    stop_event: asyncio.Event,
) -> None:
    # Keep realtime behavior while respecting board ringbuffer pressure.
    pending = [chunk]
    while pending and not stop_event.is_set():
        piece = pending.pop(0)
        if not piece:
            continue
        res = await client.post(
            inject_url,
            headers={"Content-Type": "application/octet-stream"},
            content=piece,
        )
        if res.status_code >= 400:
            detail = (res.text or "").strip()[:240]
            raise RuntimeError(f"inject failed ({res.status_code}): {detail}")
        dropped = False
        text = (res.text or "").strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
                dropped = bool(payload.get("dropped"))
            except Exception:
                dropped = False
        if not dropped:
            continue
        # Board reported buffer full; retry with smaller pieces + brief backoff.
        if len(piece) > 800:
            mid = len(piece) // 2
            pending.insert(0, piece[mid:])
            pending.insert(0, piece[:mid])
        else:
            pending.insert(0, piece)
            await asyncio.sleep(0.03)


# Match korvo-app PLAYBACK_PREBUFFER_BYTES: burst this much before pacing so the device FIFO fills.
_BOARD_PREBUFFER_BYTES = 30720
# Keep ~this many seconds of audio "ahead" of wall clock after prebuffer (absorbs HTTP jitter).
_STREAM_LEAD_SEC = 0.38


async def _pump_ffmpeg_pcm_to_board_inject(
    proc: asyncio.subprocess.Process,
    board_base: str,
    stop_event: asyncio.Event,
    progress_cb,
    chunk_size: int,
    stream_volume: float,
    pause_check: Callable[[], bool] | None,
) -> str:
    """Read s16le mono @ 16kHz from ffmpeg stdout and POST to board. Returns eof / paused / stopped."""
    vol = clamp_stream_volume(stream_volume)
    inject_url = f"{board_base}/api/audio/inject"
    bytes_per_sec = 16000 * 2
    # Slow Wi‑Fi / busy ESP can exceed 12s per chunk; avoid spurious Read/Write timeout during inject.
    timeout = httpx.Timeout(connect=15.0, read=90.0, write=90.0, pool=None)
    async with httpx.AsyncClient(timeout=timeout) as client:
        loop = asyncio.get_running_loop()
        bytes_sent = 0
        pace_t0: float | None = None
        pace_b0 = 0
        exit_reason = "eof"
        try:
            while not stop_event.is_set():
                if pause_check and pause_check():
                    exit_reason = "paused"
                    break
                if not proc.stdout:
                    break
                chunk = await proc.stdout.read(chunk_size)
                if not chunk:
                    if pause_check and pause_check():
                        exit_reason = "paused"
                    break
                if pause_check and pause_check():
                    exit_reason = "paused"
                    break
                if vol < 0.9999:
                    chunk = scale_s16le_mono_pcm(chunk, vol)
                await _post_inject_chunk_with_backpressure(client, inject_url, chunk, stop_event)
                if progress_cb:
                    progress_cb(len(chunk))
                bytes_sent += len(chunk)
                if stop_event.is_set():
                    exit_reason = "stopped"
                    break
                if bytes_sent < _BOARD_PREBUFFER_BYTES:
                    continue
                if pace_t0 is None:
                    pace_t0 = loop.time()
                    pace_b0 = bytes_sent
                    continue
                audio_done = (bytes_sent - pace_b0) / float(bytes_per_sec)
                wall = loop.time() - pace_t0
                sleep = audio_done - _STREAM_LEAD_SEC - wall
                if sleep > 0:
                    await asyncio.sleep(min(sleep, 0.28))
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except TimeoutError:
                    proc.kill()
            if proc.stderr:
                err = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
                if err and exit_reason == "eof":
                    raise RuntimeError(err[:500])
        if stop_event.is_set() and exit_reason == "eof":
            exit_reason = "stopped"
        return exit_reason


async def stream_media_file_to_board(
    board_base: str,
    source_path: Path,
    stop_event: asyncio.Event,
    progress_cb=None,
    chunk_size: int = 3200,
    stream_volume: float = 1.0,
    start_sec: float = 0.0,
    pause_check: Callable[[], bool] | None = None,
    proc_slot: list[Any] | None = None,
) -> str:
    """Stream transcoded PCM to board. Returns 'eof' (end of file), 'paused', or 'stopped'."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found (install: brew install ffmpeg)")

    cmd: list[str] = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]
    if start_sec >= 0.001:
        cmd.extend(["-ss", f"{start_sec:.3f}"])
    cmd.extend(
        [
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
        ]
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if proc_slot is not None:
        proc_slot.clear()
        proc_slot.append(proc)

    return await _pump_ffmpeg_pcm_to_board_inject(
        proc,
        board_base,
        stop_event,
        progress_cb,
        chunk_size,
        stream_volume,
        pause_check,
    )


async def stream_wav_bytes_to_board(
    board_base: str,
    wav_bytes: bytes,
    stop_event: asyncio.Event,
    progress_cb=None,
    stream_volume: float = 1.0,
) -> str:
    """Decode in-memory WAV with FFmpeg via stdin (no temp file), then stream s16le to the board."""
    if not wav_bytes:
        raise RuntimeError("empty wav payload")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found (install: brew install ffmpeg)")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "wav",
        "-i",
        "pipe:0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _feed_wav_stdin() -> None:
        if proc.stdin is None:
            return
        try:
            n = len(wav_bytes)
            off = 0
            while off < n:
                take = min(65536, n - off)
                proc.stdin.write(wav_bytes[off : off + take])
                off += take
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    feeder = asyncio.create_task(_feed_wav_stdin())
    try:
        return await _pump_ffmpeg_pcm_to_board_inject(
            proc,
            board_base,
            stop_event,
            progress_cb,
            3200,
            stream_volume,
            None,
        )
    finally:
        await feeder
