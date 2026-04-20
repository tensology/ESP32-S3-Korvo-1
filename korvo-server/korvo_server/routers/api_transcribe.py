"""WebSocket: Korvo board WAV stream → chunked local Whisper → JSON partial transcripts."""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from korvo_server.korvo_pcm import WavStreamToPcm16
from korvo_server.routers.api_audio import _allowed_upstream
from korvo_server import whisper_stt
from korvo_server.transcript_dedupe import sliding_window_text_delta

log = logging.getLogger(__name__)
router = APIRouter(tags=["audio"])

_PCM_RATE = 16000
_BYTES_MONO_S16_1S = _PCM_RATE * 2

# One inference at a time across all connections (whisper.cpp model is not proven thread-safe).
_ws_whisper_lock: asyncio.Lock | None = None


def _decode_board_url(raw: str) -> str:
    s = (raw or "").strip()
    for _ in range(4):
        nxt = unquote(s)
        if nxt == s:
            break
        s = nxt
    return s.strip()


@router.websocket("/ws/audio/transcribe")
async def ws_audio_transcribe(websocket: WebSocket) -> None:
    qp = websocket.query_params
    board_url = _decode_board_url(qp.get("board_url") or "")

    if not board_url:
        await websocket.close(code=1008)
        return
    if not _allowed_upstream(board_url):
        await websocket.close(code=1008)
        return
    if not whisper_stt.whisper_ready():
        await websocket.close(code=1013)
        return

    await websocket.accept()

    model_id = (qp.get("model") or "base.en").strip() or "base.en"
    try:
        step_sec = float(qp.get("step_sec") or "1.25")
    except ValueError:
        step_sec = 1.25
    try:
        window_sec = float(qp.get("window_sec") or "5.0")
    except ValueError:
        window_sec = 5.0
    step_sec = max(0.75, min(step_sec, 30.0))
    window_sec = max(1.5, min(window_sec, 60.0))

    try:
        whisper_stt.get_whisper_model(model_id)
    except Exception as e:  # noqa: BLE001
        try:
            await websocket.send_json({"type": "error", "message": f"Model load failed: {e}"})
        except Exception:
            pass
        await websocket.close(code=1011)
        return

    try:
        await websocket.send_json({
            "type": "ready",
            "model": model_id,
            "step_sec": step_sec,
            "window_sec": window_sec,
            "pcm": "mono_s16le_16khz",
        })
    except Exception:
        return

    global _ws_whisper_lock
    if _ws_whisper_lock is None:
        _ws_whisper_lock = asyncio.Lock()

    pcm_buf = bytearray()
    parser = WavStreamToPcm16()
    window_bytes = int(_PCM_RATE * 2 * window_sec)
    max_buf_bytes = int(_PCM_RATE * 2 * 90)
    last_run = 0.0
    last_window_transcript = ""

    timeout = httpx.Timeout(connect=20.0, read=None, write=20.0, pool=None)
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=4)
    headers = {"Connection": "close", "Accept": "*/*", "User-Agent": "korvo-server/transcribe-ws"}

    def _connected() -> bool:
        return websocket.client_state == WebSocketState.CONNECTED

    try:
        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
            try:
                async with client.stream("GET", board_url, headers=headers) as resp:
                    if resp.status_code != 200:
                        detail = (await resp.aread())[:800].decode(errors="replace")
                        if _connected():
                            await websocket.send_json({"type": "error", "message": f"Upstream HTTP {resp.status_code}: {detail}"})
                        return
                    try:
                        async for chunk in resp.aiter_bytes(16384):
                            if not _connected():
                                break
                            pcm = parser.feed(chunk)
                            if pcm:
                                pcm_buf.extend(pcm)
                                if len(pcm_buf) > max_buf_bytes:
                                    del pcm_buf[: len(pcm_buf) - max_buf_bytes]
                            now = time.monotonic()
                            if len(pcm_buf) < int(_BYTES_MONO_S16_1S * 0.9):
                                continue
                            if now - last_run < step_sec:
                                continue
                            last_run = now
                            win = bytes(pcm_buf[-window_bytes:]) if len(pcm_buf) >= window_bytes else bytes(pcm_buf)
                            async with _ws_whisper_lock:
                                try:
                                    text = await asyncio.to_thread(whisper_stt.transcribe_pcm16_mono_s16le, win, model_id)
                                except Exception as e:  # noqa: BLE001
                                    log.exception("whisper transcribe failed")
                                    if _connected():
                                        await websocket.send_json({"type": "error", "message": str(e)})
                                    continue
                            if not _connected():
                                break
                            delta = sliding_window_text_delta(last_window_transcript, text)
                            last_window_transcript = text
                            await websocket.send_json({
                                "type": "partial",
                                "text": text,
                                "delta": delta,
                                "window_sec": window_sec,
                                "t_unix": time.time(),
                            })
                    except httpx.HTTPError as e:
                        log.warning("transcribe board stream read ended: %s", e)
                        if _connected():
                            try:
                                await websocket.send_json({"type": "error", "message": f"Stream read error: {e}"})
                            except Exception:
                                pass
            except httpx.HTTPError as e:
                log.warning("transcribe board stream open failed: %s", e)
                if _connected():
                    try:
                        await websocket.send_json({"type": "error", "message": f"Upstream connection failed: {e}"})
                    except Exception:
                        pass
    except WebSocketDisconnect:
        return
    except httpx.HTTPError as e:
        log.warning("transcribe httpx: %s", e)
        if _connected():
            try:
                await websocket.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        log.exception("transcribe ws")
        if _connected():
            try:
                await websocket.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass
    finally:
        try:
            if _connected():
                await websocket.send_json({"type": "done"})
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
