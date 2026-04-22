from __future__ import annotations

import array
import asyncio
import json
import re
import shutil
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def _build_board_base_candidates_sync(board_base: str) -> list[str]:
    """
    Return preferred board targets for inject calls.
    First is the original base; additional candidates include resolved IPv4 for mDNS hosts.
    """
    base = normalize_board_base(board_base)
    if not base:
        return []
    candidates = [base]
    try:
        parsed = urlparse(base)
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not host or not host.endswith(".local"):
            return candidates
        port = parsed.port
        scheme = parsed.scheme or "http"
        infos = socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
        seen: set[str] = set()
        for info in infos:
            ip = (info[4] or [""])[0]
            if not ip or ":" in ip:
                continue
            if ip in seen:
                continue
            seen.add(ip)
            if port:
                candidates.append(f"{scheme}://{ip}:{port}")
            else:
                candidates.append(f"{scheme}://{ip}")
    except Exception:
        pass
    return candidates


async def _build_board_base_candidates(board_base: str) -> list[str]:
    """
    Resolve candidate targets off the event loop so mDNS lookup cannot stall FastAPI.
    """
    try:
        return await asyncio.wait_for(asyncio.to_thread(_build_board_base_candidates_sync, board_base), timeout=0.9)
    except Exception:
        base = normalize_board_base(board_base)
        return [base] if base else []


def _rotate_board_target(telemetry_state: dict[str, Any]) -> bool:
    candidates = telemetry_state.get("board_base_candidates") or []
    if not candidates:
        return False
    idx = int(telemetry_state.get("board_base_idx", 0))
    if (idx + 1) >= len(candidates):
        return False
    next_idx = idx + 1
    telemetry_state["board_base_idx"] = next_idx
    telemetry_state["active_board_base"] = candidates[next_idx]
    return True


def _is_retryable_transport_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.ConnectError,
        ),
    )


async def _post_inject_chunk_with_backpressure(
    client: httpx.AsyncClient,
    chunk: bytes,
    stop_event: asyncio.Event,
    telemetry_state: dict[str, Any],
) -> None:
    # Keep realtime behavior while respecting board ringbuffer pressure.
    pending = [chunk]
    while pending and not stop_event.is_set():
        piece = pending.pop(0)
        if not piece:
            continue
        res = None
        while True:
            active_board_base = telemetry_state.get("active_board_base") or ""
            if not active_board_base:
                raise RuntimeError("missing active board base")
            inject_url = f"{active_board_base}/api/audio/inject"
            try:
                res = await client.post(
                    inject_url,
                    headers={"Content-Type": "application/octet-stream", "Connection": "close"},
                    content=piece,
                )
                break
            except Exception as exc:
                if not _is_retryable_transport_error(exc):
                    raise
                # Transient board-side disconnects under pressure: retry with backoff
                # and progressively smaller pieces; rotate target if available.
                if len(piece) > 800:
                    mid = len(piece) // 2
                    pending.insert(0, piece[mid:])
                    pending.insert(0, piece[:mid])
                else:
                    pending.insert(0, piece)
                rotated = _rotate_board_target(telemetry_state)
                await asyncio.sleep(0.04 if rotated else 0.06)
                break
        if res is None:
            # Retry path re-queued this piece (or split pieces); do not dereference response.
            continue
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
            telemetry_state["consecutive_drop_responses"] = 0
            continue
        telemetry_state["consecutive_drop_responses"] = int(telemetry_state.get("consecutive_drop_responses", 0)) + 1
        # Board reported buffer full; retry with smaller pieces + brief backoff.
        if len(piece) > 800:
            mid = len(piece) // 2
            pending.insert(0, piece[mid:])
            pending.insert(0, piece[:mid])
        else:
            pending.insert(0, piece)
            await asyncio.sleep(0.03)
    await _apply_adaptive_backpressure(client, telemetry_state)


async def _apply_adaptive_backpressure(
    client: httpx.AsyncClient,
    telemetry_state: dict[str, Any],
) -> None:
    """
    Pull board inject telemetry and add short sleeps only when queue pressure is high.
    This avoids fixed timer gates while keeping output stable under stress.
    """
    now = time.monotonic()
    next_poll_at = float(telemetry_state.get("next_poll_at", 0.0))
    if now < next_poll_at:
        return
    telemetry_state["next_poll_at"] = now + 0.18
    try:
        active_board_base = telemetry_state.get("active_board_base") or ""
        if not active_board_base:
            return
        status_url = f"{active_board_base}/api/audio/inject/status"
        res = None
        while True:
            try:
                res = await client.get(status_url)
                break
            except Exception as exc:
                if _is_retryable_transport_error(exc) and _rotate_board_target(telemetry_state):
                    active_board_base = telemetry_state.get("active_board_base") or ""
                    if not active_board_base:
                        return
                    status_url = f"{active_board_base}/api/audio/inject/status"
                    continue
                return
        if res is None:
            return
        if res.status_code >= 400:
            return
        data = res.json()
    except Exception:
        # Do not break audio flow if telemetry endpoint is unavailable.
        return

    queue_bytes = int(data.get("queue_bytes_est", 0) or 0)
    fifo_level = int(data.get("fifo_level", 0) or 0)
    dropped_posts = int(data.get("posts_dropped", 0) or 0)
    prev_dropped = int(telemetry_state.get("posts_dropped", dropped_posts))
    telemetry_state["posts_dropped"] = dropped_posts
    drop_delta = max(0, dropped_posts - prev_dropped)
    forced_drops = int(telemetry_state.get("consecutive_drop_responses", 0))

    # Pressure policy:
    # - Light pressure: tiny pause
    # - Medium/high pressure or fresh drops: stronger pause to let playback catch up
    pause_sec = 0.0
    if queue_bytes >= 120000 or fifo_level >= 26000:
        pause_sec = 0.09
    elif queue_bytes >= 90000 or fifo_level >= 21000:
        pause_sec = 0.055
    elif queue_bytes >= 70000 or fifo_level >= 16000:
        pause_sec = 0.028
    if drop_delta > 0 or forced_drops > 0:
        pause_sec = max(pause_sec, 0.06 + min(0.12, 0.02 * (drop_delta + forced_drops)))
    if pause_sec > 0.0:
        await asyncio.sleep(pause_sec)


# Match korvo-app PLAYBACK_PREBUFFER_BYTES: burst this much before pacing so the device FIFO fills.
_BOARD_PREBUFFER_BYTES = 12800
# Keep a modest lead after prebuffer to absorb network jitter without adding noticeable latency.
_STREAM_LEAD_SEC = 0.20


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
    board_base_candidates = await _build_board_base_candidates(board_base)
    active_board_base = board_base_candidates[0] if board_base_candidates else board_base
    bytes_per_sec = 16000 * 2
    # For realtime TTS, fail fast on connect stalls so stale requests do not
    # block newer utterances for 15+ seconds.
    timeout = httpx.Timeout(connect=6.0, read=45.0, write=45.0, pool=None)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=0)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        loop = asyncio.get_running_loop()
        bytes_sent = 0
        pace_t0: float | None = None
        pace_b0 = 0
        exit_reason = "eof"
        telemetry_state: dict[str, Any] = {
            "next_poll_at": 0.0,
            "board_base_candidates": board_base_candidates,
            "board_base_idx": 0,
            "active_board_base": active_board_base,
        }
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
                await _post_inject_chunk_with_backpressure(
                    client,
                    chunk,
                    stop_event,
                    telemetry_state,
                )
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
