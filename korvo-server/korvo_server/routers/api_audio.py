import asyncio
import logging
import math
import re
import shutil
import struct
import subprocess
import json
import threading
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from korvo_server.audio_push import clamp_stream_volume, normalize_board_base, stream_media_file_to_board
from korvo_server.audio_hub import audio_hub
from korvo_server.config import RECORDINGS_DIR

router = APIRouter(tags=["audio"])
log = logging.getLogger(__name__)


@dataclass
class LiveRecordingSession:
    session_id: str
    wav_path: Path
    mp3_path: Path
    task: asyncio.Task
    stop_event: asyncio.Event
    started_at: datetime


_live_recording_lock = threading.Lock()
_live_recording: LiveRecordingSession | None = None


@dataclass
class PushPlaybackSession:
    session_id: str
    source_name: str
    board_url: str
    started_at: datetime
    stop_event: asyncio.Event
    task: asyncio.Task | None = None
    bytes_sent: int = 0
    chunks_sent: int = 0
    done: bool = False
    stopped: bool = False
    error: str | None = None
    temp_path: Path | None = None
    duration_sec: float | None = None
    position_sec: float = 0.0
    paused: bool = False
    stream_vol: float = 1.0
    ffmpeg_proc_slot: list = field(default_factory=list)


_push_playback_lock = threading.Lock()
_push_playback: PushPlaybackSession | None = None


def _duration_from_ffmpeg_stderr(stderr: str) -> float | None:
    """Parse 'Duration: HH:MM:SS.xx' from ffmpeg -i stderr (fallback when ffprobe fails)."""
    if not stderr:
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
    if not m:
        return None
    try:
        h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return float(h * 3600 + mn * 60 + s)
    except (TypeError, ValueError):
        return None


def _ffprobe_duration_sync(path: Path) -> float | None:
    if not path.is_file():
        return None
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        # JSON format is often more reliable than default=noprint_wrappers across file types.
        try:
            cp = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_format",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if cp.returncode == 0 and (cp.stdout or "").strip():
                payload = json.loads(cp.stdout)
                raw = (payload.get("format") or {}).get("duration")
                if raw is not None and str(raw).strip().upper() != "N/A":
                    d = float(raw)
                    if d > 0 and math.isfinite(d):
                        return d
        except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, KeyError):
            pass
        try:
            cp = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if cp.returncode != 0:
                return None
            line = (cp.stdout or "").strip().splitlines()[0] if (cp.stdout or "").strip() else ""
            if not line or line.upper() == "N/A":
                return None
            d = float(line)
            if d > 0 and math.isfinite(d):
                return d
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            cp = subprocess.run(
                [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-f", "null", "-"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            d = _duration_from_ffmpeg_stderr(cp.stderr or "")
            if d is not None and d > 0 and math.isfinite(d):
                return d
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def _terminate_session_ffmpeg(session: PushPlaybackSession) -> None:
    slot = session.ffmpeg_proc_slot
    if not slot:
        return
    for proc in list(slot):
        if proc is None:
            continue
        try:
            if proc.returncode is None:
                proc.terminate()
        except ProcessLookupError:
            pass


def _is_dropped_payload(text: str) -> bool:
    t = (text or "").strip()
    if not t.startswith("{"):
        return False
    try:
        payload = json.loads(t)
    except Exception:
        return False
    return bool(payload.get("dropped"))


def _allowed_upstream(url: str) -> bool:
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if not p.hostname:
        return False
    hn = p.hostname.lower().rstrip(".")
    if hn in ("localhost", "127.0.0.1", "korvo.local", "0.0.0.0"):
        return True
    if hn.endswith(".local"):
        return True
    if hn.startswith("192.168."):
        return True
    if hn.startswith("10."):
        return True
    if hn.startswith("172."):
        return True
    return False


def _finalize_wav_header(path: Path) -> None:
    """Patch RIFF and data chunk sizes so VLC/ffplay accept the file (board uses 0xFFFFFFFF placeholders)."""
    size = path.stat().st_size
    if size < 44:
        return
    with path.open("r+b") as f:
        hdr = bytearray(f.read(44))
        if hdr[0:4] != b"RIFF" or hdr[8:12] != b"WAVE" or hdr[36:40] != b"data":
            return
        struct.pack_into("<I", hdr, 4, size - 8)
        struct.pack_into("<I", hdr, 40, size - 44)
        f.seek(0)
        f.write(hdr)


def _safe_basename(name: str | None) -> str | None:
    if not name or not name.strip():
        return None
    base = Path(name.strip()).name
    if not re.match(r"^[a-zA-Z0-9._-]+$", base) or ".." in base:
        return None
    if not base.lower().endswith(".wav"):
        base = base + ".wav"
    return base


async def _record_board_stream(
    board_url: str,
    out_path: Path,
    duration_sec: float,
    play_ffplay: bool,
) -> int:
    """Pull board WAV stream to disk; optionally tee bytes into ffplay stdin. Returns bytes written."""
    timeout = httpx.Timeout(connect=25.0, read=None, write=25.0, pool=None)
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
    headers = {"Connection": "close", "Accept": "*/*", "User-Agent": "korvo-server/record"}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.5, min(duration_sec, 600.0))

    ffplay: asyncio.subprocess.Process | None = None
    if play_ffplay:
        ff = shutil.which("ffplay")
        if not ff:
            raise HTTPException(400, "ffplay not found (install ffmpeg: brew install ffmpeg) — record without ffplay=1 or install ffplay.")
        ffplay = await asyncio.create_subprocess_exec(
            ff,
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "error",
            "-f",
            "wav",
            "-i",
            "pipe:0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    total = 0
    try:
        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
            async with client.stream("GET", board_url, headers=headers) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread())[:1200].decode(errors="replace")
                    raise HTTPException(resp.status_code, f"Board stream failed: {detail}")
                with out_path.open("wb") as out:
                    async for chunk in resp.aiter_bytes(32768):
                        if loop.time() >= deadline:
                            break
                        if not chunk:
                            continue
                        out.write(chunk)
                        total += len(chunk)
                        if ffplay and ffplay.stdin:
                            ffplay.stdin.write(chunk)
                            await ffplay.stdin.drain()
    finally:
        if ffplay is not None:
            if ffplay.stdin:
                try:
                    ffplay.stdin.close()
                except (BrokenPipeError, ConnectionResetError, RuntimeError):
                    pass
            try:
                await asyncio.wait_for(ffplay.wait(), timeout=8.0)
            except TimeoutError:
                ffplay.kill()

    if total >= 44:
        _finalize_wav_header(out_path)
    elif out_path.exists():
        try:
            out_path.unlink()
        except OSError:
            pass
    return total


async def _record_board_stream_until_stop(
    board_url: str,
    out_path: Path,
    stop_event: asyncio.Event,
) -> int:
    """Pull board WAV stream to disk until stop_event is set."""
    timeout = httpx.Timeout(connect=25.0, read=None, write=25.0, pool=None)
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
    headers = {"Connection": "close", "Accept": "*/*", "User-Agent": "korvo-server/live-record"}

    total = 0
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        async with client.stream("GET", board_url, headers=headers) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread())[:1200].decode(errors="replace")
                raise HTTPException(resp.status_code, f"Board stream failed: {detail}")
            with out_path.open("wb") as out:
                async for chunk in resp.aiter_bytes(32768):
                    if stop_event.is_set():
                        break
                    if not chunk:
                        continue
                    out.write(chunk)
                    total += len(chunk)

    if total >= 44:
        _finalize_wav_header(out_path)
    elif out_path.exists():
        try:
            out_path.unlink()
        except OSError:
            pass
    return total


async def _convert_wav_to_mp3(wav_path: Path, mp3_path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(mp3_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await proc.wait()
    return rc == 0 and mp3_path.exists() and mp3_path.stat().st_size > 0


def _safe_stem(name: str | None) -> str | None:
    if not name or not name.strip():
        return None
    stem = Path(name.strip()).stem
    if not re.match(r"^[a-zA-Z0-9._-]+$", stem) or ".." in stem:
        return None
    return stem


class RecordBody(BaseModel):
    board_url: str = Field(..., description="e.g. http://192.168.1.27/api/audio/stream")
    duration_sec: float = Field(30.0, ge=0.5, le=600.0)
    play_ffplay: bool = Field(False, description="If true, also play through ffplay (same TCP stream as file).")
    filename: str | None = Field(None, description="Optional .wav basename (letters, digits, ._- only).")


class LiveRecordStartBody(BaseModel):
    board_url: str = Field(..., description="e.g. http://192.168.1.27/api/audio/stream")
    filename_stem: str | None = Field(None, description="Optional output stem (letters, digits, ._- only).")


class PushFileSeekBody(BaseModel):
    position_sec: float = Field(0.0, ge=0.0)


@router.post("/api/audio/record/live/start")
async def record_live_start(body: LiveRecordStartBody):
    """Start a long-running recording session; stop via /api/audio/record/live/stop."""
    global _live_recording
    if not _allowed_upstream(body.board_url):
        raise HTTPException(400, "board_url host not allowed.")
    with _live_recording_lock:
        if _live_recording is not None:
            raise HTTPException(409, "A live recording session is already running.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = _safe_stem(body.filename_stem) or f"korvo_live_{stamp}"
    wav_path = RECORDINGS_DIR / f"{stem}.wav"
    mp3_path = RECORDINGS_DIR / f"{stem}.mp3"
    stop_event = asyncio.Event()
    task = asyncio.create_task(_record_board_stream_until_stop(body.board_url, wav_path, stop_event))
    session = LiveRecordingSession(
        session_id=str(uuid.uuid4()),
        wav_path=wav_path,
        mp3_path=mp3_path,
        task=task,
        stop_event=stop_event,
        started_at=datetime.now(timezone.utc),
    )
    with _live_recording_lock:
        _live_recording = session
    return {
        "ok": True,
        "session_id": session.session_id,
        "started_at": session.started_at.isoformat(),
        "wav_filename": session.wav_path.name,
    }


@router.post("/api/audio/record/live/stop")
async def record_live_stop(request: Request):
    """Stop current recording session, finalize WAV, and try to export MP3."""
    global _live_recording
    with _live_recording_lock:
        session = _live_recording
        _live_recording = None
    if session is None:
        raise HTTPException(404, "No active live recording session.")

    session.stop_event.set()
    try:
        bytes_written = await asyncio.wait_for(session.task, timeout=20.0)
    except TimeoutError:
        session.task.cancel()
        raise HTTPException(500, "Recording stop timed out.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Recording failed: {e}") from e

    mp3_ok = False
    if session.wav_path.exists() and session.wav_path.stat().st_size > 44:
        mp3_ok = await _convert_wav_to_mp3(session.wav_path, session.mp3_path)

    base = str(request.base_url).rstrip("/")
    out = {
        "ok": True,
        "session_id": session.session_id,
        "bytes_written": bytes_written,
        "wav_filename": session.wav_path.name if session.wav_path.exists() else None,
        "wav_url": f"{base}/recordings/{session.wav_path.name}" if session.wav_path.exists() else None,
        "mp3_filename": session.mp3_path.name if mp3_ok else None,
        "mp3_url": f"{base}/recordings/{session.mp3_path.name}" if mp3_ok else None,
        "mp3_available": mp3_ok,
    }
    if not mp3_ok:
        out["message"] = "ffmpeg not found or MP3 conversion failed; WAV is available."
    return out


@router.get("/api/audio/relay")
async def relay_board_audio(
    request: Request,
    url: str = Query(..., description="Board stream URL, e.g. http://192.168.1.5/api/audio/stream"),
):
    if not _allowed_upstream(url):
        raise HTTPException(400, "Relay URL host not allowed (use LAN / korvo.local / localhost).")
    client_host = request.client.host if request.client else ""

    async def stream():
        async for chunk in audio_hub.subscribe(url):
            if chunk:
                yield chunk

    return StreamingResponse(
        stream(),
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
            "X-Relay-Client": client_host,
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/audio/record")
async def record_get(
    request: Request,
    board_url: str = Query(..., description="Board GET /api/audio/stream URL"),
    duration_sec: float = Query(30.0, ge=0.5, le=600.0),
    ffplay: bool = Query(False, description="Play audio while recording (requires ffplay in PATH)"),
    filename: str | None = Query(None, description="Optional output basename (.wav)"),
):
    """Pull the Korvo WAV stream into korvo-server/recordings/*.wav (VLC-safe header). Optional live ffplay."""
    if not _allowed_upstream(board_url):
        raise HTTPException(400, "board_url host not allowed.")
    safe = _safe_basename(filename)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_name = safe or f"korvo_{stamp}.wav"
    out_path = RECORDINGS_DIR / out_name
    bytes_written = await _record_board_stream(board_url, out_path, duration_sec, ffplay)
    rel_url = f"{str(request.base_url).rstrip('/')}/recordings/{out_path.name}"
    return {
        "ok": True,
        "path": str(out_path),
        "filename": out_path.name,
        "bytes_written": bytes_written,
        "duration_sec": duration_sec,
        "play_url": rel_url,
        "vlc_hint": f'Open in VLC: vlc "{out_path}"',
    }


@router.post("/api/audio/record")
async def record_post(request: Request, body: RecordBody):
    """Same as GET /api/audio/record with JSON body."""
    if not _allowed_upstream(body.board_url):
        raise HTTPException(400, "board_url host not allowed.")
    safe = _safe_basename(body.filename)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_name = safe or f"korvo_{stamp}.wav"
    out_path = RECORDINGS_DIR / out_name
    bytes_written = await _record_board_stream(body.board_url, out_path, body.duration_sec, body.play_ffplay)
    rel_url = f"{str(request.base_url).rstrip('/')}/recordings/{out_path.name}"
    return {
        "ok": True,
        "path": str(out_path),
        "filename": out_path.name,
        "bytes_written": bytes_written,
        "duration_sec": body.duration_sec,
        "play_url": rel_url,
        "vlc_hint": f'Open in VLC: vlc "{out_path}"',
    }


@router.post("/api/audio/push-file/start")
async def push_file_start(
    board_url: str = Form(...),
    audio_file: UploadFile = File(...),
    stream_volume: str = Form("1"),
):
    global _push_playback
    base = normalize_board_base(board_url)
    if not base:
        raise HTTPException(400, "board_url is required")
    if not _allowed_upstream(base):
        raise HTTPException(400, "board_url host not allowed")
    with _push_playback_lock:
        if _push_playback and not _push_playback.done:
            raise HTTPException(409, "A file stream is already running")

    suffix = Path(audio_file.filename or "upload.bin").suffix or ".bin"
    temp = RECORDINGS_DIR / f"push_{uuid.uuid4().hex}{suffix}"
    data = await audio_file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty")
    temp.write_bytes(data)
    try:
        vol = float((stream_volume or "1").strip())
    except ValueError:
        vol = 1.0
    vol = clamp_stream_volume(vol)
    dur = await asyncio.to_thread(_ffprobe_duration_sync, temp)
    stop_event = asyncio.Event()
    session = PushPlaybackSession(
        session_id=str(uuid.uuid4()),
        source_name=audio_file.filename or temp.name,
        board_url=base,
        started_at=datetime.now(timezone.utc),
        stop_event=stop_event,
        task=None,  # type: ignore[arg-type]
        temp_path=temp,
        duration_sec=dur,
        position_sec=0.0,
        paused=False,
        stream_vol=vol,
    )

    async def _runner() -> None:
        current_offset = 0.0
        try:
            while not session.stop_event.is_set():
                segment_pcm = [0]

                def _on_progress(sent: int) -> None:
                    session.bytes_sent += sent
                    session.chunks_sent += 1
                    segment_pcm[0] += sent
                    session.position_sec = current_offset + segment_pcm[0] / 32000.0

                # Do not clear session.paused here — that races with /pause and breaks pause/resume.
                reason = await stream_media_file_to_board(
                    base,
                    temp,
                    session.stop_event,
                    progress_cb=_on_progress,
                    stream_volume=session.stream_vol,
                    start_sec=current_offset,
                    pause_check=lambda: session.paused,
                    proc_slot=session.ffmpeg_proc_slot,
                )
                if session.stop_event.is_set():
                    break
                if reason == "paused":
                    current_offset = session.position_sec
                    while session.paused and not session.stop_event.is_set():
                        await asyncio.sleep(0.05)
                    if session.stop_event.is_set():
                        break
                    current_offset = session.position_sec
                    continue
                break
        except Exception as e:
            session.error = str(e)
        finally:
            session.done = True
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    session.task = asyncio.create_task(_runner())
    with _push_playback_lock:
        _push_playback = session
    return {
        "ok": True,
        "session_id": session.session_id,
        "source_name": session.source_name,
        "board_url": session.board_url,
        "duration_sec": session.duration_sec,
    }


@router.post("/api/audio/push-file/stop")
async def push_file_stop():
    with _push_playback_lock:
        session = _push_playback
    if not session or session.done:
        return {"ok": True, "stopped": False}
    session.stopped = True
    session.stop_event.set()
    _terminate_session_ffmpeg(session)
    return {"ok": True, "stopped": True}


@router.post("/api/audio/push-file/pause")
async def push_file_pause():
    with _push_playback_lock:
        session = _push_playback
    if not session or session.done:
        raise HTTPException(404, "No active file stream.")
    if session.paused:
        return {"ok": True, "paused": True, "position_sec": session.position_sec}
    session.paused = True
    _terminate_session_ffmpeg(session)
    return {"ok": True, "paused": True, "position_sec": session.position_sec}


@router.post("/api/audio/push-file/resume")
async def push_file_resume():
    with _push_playback_lock:
        session = _push_playback
    if not session or session.done:
        raise HTTPException(404, "No active file stream.")
    if not session.paused:
        return {"ok": True, "paused": False, "position_sec": session.position_sec}
    session.paused = False
    return {"ok": True, "paused": False, "position_sec": session.position_sec}


@router.post("/api/audio/push-file/seek")
async def push_file_seek(body: PushFileSeekBody):
    with _push_playback_lock:
        session = _push_playback
    if not session or session.done:
        raise HTTPException(404, "No active file stream.")
    dur = session.duration_sec
    pos = body.position_sec
    if dur is not None and dur > 0:
        pos = min(max(0.0, pos), dur)
    else:
        pos = max(0.0, pos)
    was_paused = session.paused
    session.position_sec = pos
    session.paused = True
    _terminate_session_ffmpeg(session)
    await asyncio.sleep(0.12)
    session.paused = was_paused
    return {"ok": True, "position_sec": session.position_sec, "paused": session.paused}


@router.get("/api/audio/push-file/status")
async def push_file_status():
    with _push_playback_lock:
        session = _push_playback
    if not session:
        return {"active": False}
    return {
        "active": not session.done,
        "session_id": session.session_id,
        "source_name": session.source_name,
        "board_url": session.board_url,
        "bytes_sent": session.bytes_sent,
        "chunks_sent": session.chunks_sent,
        "done": session.done,
        "stopped": session.stopped,
        "error": session.error,
        "started_at": session.started_at.isoformat(),
        "duration_sec": session.duration_sec,
        "position_sec": session.position_sec,
        "paused": session.paused,
    }


@router.post("/api/audio/ingest")
async def ingest_audio(request: Request, max_bytes: int = Query(1_000_000, ge=1024, le=50_000_000)):
    """Debug endpoint: accept raw audio/octet-stream body (count bytes, do not persist)."""
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total >= max_bytes:
            break
    return {"received_bytes": total, "capped_at": max_bytes}


@router.websocket("/ws/audio/push-mic")
async def push_mic_ws(ws: WebSocket):
    board_url = normalize_board_base((ws.query_params.get("board_url") or "").strip())
    if not board_url or not _allowed_upstream(board_url):
        await ws.close(code=1008, reason="Invalid board_url")
        return
    await ws.accept()
    inject_url = f"{board_url}/api/audio/inject"
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=24)
    running = True

    async def _sender() -> None:
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=None)
        async with httpx.AsyncClient(timeout=timeout) as client:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if not item:
                    continue
                pending = [item]
                while pending:
                    chunk = pending.pop(0)
                    try:
                        res = await client.post(
                            inject_url,
                            headers={"Content-Type": "application/octet-stream"},
                            content=chunk,
                        )
                    except Exception:
                        continue
                    if res.status_code >= 400:
                        continue
                    if not _is_dropped_payload(res.text):
                        continue
                    if len(chunk) > 800:
                        mid = len(chunk) // 2
                        pending.insert(0, chunk[mid:])
                        pending.insert(0, chunk[:mid])
                    else:
                        pending.insert(0, chunk)
                        await asyncio.sleep(0.02)

    sender_task = asyncio.create_task(_sender())
    try:
        while running:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if not data:
                continue
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # Keep low-latency realtime behavior: drop one old packet.
                try:
                    _ = queue.get_nowait()
                except Exception:
                    pass
                try:
                    queue.put_nowait(data)
                except Exception:
                    pass
    except WebSocketDisconnect:
        running = False
    finally:
        try:
            queue.put_nowait(None)
        except Exception:
            pass
        try:
            await asyncio.wait_for(sender_task, timeout=3.0)
        except Exception:
            sender_task.cancel()
