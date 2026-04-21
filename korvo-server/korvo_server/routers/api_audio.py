import asyncio
import logging
import re
import shutil
import struct
import threading
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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


@router.post("/api/audio/ingest")
async def ingest_audio(request: Request, max_bytes: int = Query(1_000_000, ge=1024, le=50_000_000)):
    """Debug endpoint: accept raw audio/octet-stream body (count bytes, do not persist)."""
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total >= max_bytes:
            break
    return {"received_bytes": total, "capped_at": max_bytes}
