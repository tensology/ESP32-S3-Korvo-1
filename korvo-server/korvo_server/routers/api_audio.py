import asyncio
import re
import shutil
import struct
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from korvo_server.config import RECORDINGS_DIR

router = APIRouter(tags=["audio"])


def _allowed_upstream(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if not p.hostname:
        return False
    hn = p.hostname.lower()
    if hn in ("localhost", "127.0.0.1", "korvo.local", "0.0.0.0"):
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


class RecordBody(BaseModel):
    board_url: str = Field(..., description="e.g. http://192.168.1.27/api/audio/stream")
    duration_sec: float = Field(30.0, ge=0.5, le=600.0)
    play_ffplay: bool = Field(False, description="If true, also play through ffplay (same TCP stream as file).")
    filename: str | None = Field(None, description="Optional .wav basename (letters, digits, ._- only).")


@router.get("/api/audio/relay")
async def relay_board_audio(
    request: Request,
    url: str = Query(..., description="Board stream URL, e.g. http://192.168.1.5/api/audio/stream"),
):
    if not _allowed_upstream(url):
        raise HTTPException(400, "Relay URL host not allowed (use LAN / korvo.local / localhost).")
    client_host = request.client.host if request.client else ""

    async def stream():
        timeout = httpx.Timeout(connect=20.0, read=None, write=20.0, pool=None)
        limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
        headers = {
            "Connection": "close",
            "Accept": "*/*",
            "User-Agent": "korvo-server/relay",
        }
        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread())[:1200].decode(errors="replace")
                    raise HTTPException(resp.status_code, f"Upstream: {detail}")
                async for chunk in resp.aiter_bytes(16384):
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
