"""WebSocket: Korvo board WAV stream → chunked local Whisper → JSON partial transcripts."""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
from collections import deque
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from korvo_server.audio_hub import audio_hub
from korvo_server.korvo_pcm import WavStreamToPcm16
from korvo_server.routers.api_audio import _allowed_upstream
from korvo_server import whisper_stt
from korvo_server.transcript_dedupe import sliding_window_text_delta
from korvo_server.tts_echo_guard import is_suppressed

log = logging.getLogger(__name__)
router = APIRouter(tags=["audio"])

_PCM_RATE = 16000
_BYTES_MONO_S16_1S = _PCM_RATE * 2
_SENTENCE_END_RE = re.compile(r"[.!?。！？]\s*$")


def _rms_pcm16le(buf: bytes) -> float:
    if not buf:
        return 0.0
    n = len(buf) // 2
    if n <= 0:
        return 0.0
    try:
        samples = struct.unpack("<" + "h" * n, buf[: n * 2])
    except Exception:
        return 0.0
    acc = 0.0
    for s in samples:
        v = float(s) / 32768.0
        acc += v * v
    return (acc / float(n)) ** 0.5


def _norm_sentence(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def _token_jaccard(a: str, b: str) -> float:
    sa = set(_norm_sentence(a).split())
    sb = set(_norm_sentence(b).split())
    if not sa or not sb:
        return 0.0
    inter = len(sa.intersection(sb))
    union = len(sa) + len(sb) - inter
    return float(inter) / float(union) if union else 0.0

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
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Missing board_url query parameter."})
        await websocket.close(code=1008)
        return
    if not _allowed_upstream(board_url):
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "board_url host not allowed (use LAN / korvo.local / localhost)."})
        await websocket.close(code=1008)
        return
    if not whisper_stt.whisper_ready():
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Whisper backend is not ready. Install/verify pywhispercpp and restart server."})
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
    sentence_buf = ""
    sentence_seq = 0
    # Activity-based endpointing (simple RMS VAD):
    # - mark "speech active" only after consecutive voiced chunks
    # - emit sentence only after sustained silence while speech was active
    speech_active = False
    voiced_streak = 0
    silence_streak = 0
    vad_voiced_rms = 0.012
    vad_voiced_chunks = 2
    vad_silence_chunks = 5
    # "Serious silence" gate to clear stale UI sentence on the frontend (server is source of truth).
    silence_clear_chunks = max(3, int(round(5.0 / step_sec)))
    # Hard utterance reset on prolonged silence to prevent stale sentence appends.
    hard_reset_chunks = max(3, int(round(4.0 / step_sec)))
    silence_clear_emitted = False
    recent_sentences = deque(maxlen=8)

    def _connected() -> bool:
        return websocket.client_state == WebSocketState.CONNECTED

    try:
        async for chunk in audio_hub.subscribe(board_url):
            if not _connected():
                break
            if is_suppressed(board_url):
                # During local TTS playback/cooldown, skip ASR windows to prevent echo feedback loops.
                last_window_transcript = ""
                continue
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
            # Tail-only RMS for VAD (reduces stale voiced energy from older window content).
            tail_bytes = int(_PCM_RATE * 2 * min(0.8, step_sec))
            tail = bytes(pcm_buf[-tail_bytes:]) if len(pcm_buf) >= tail_bytes else bytes(pcm_buf)
            rms = _rms_pcm16le(tail)
            if rms >= vad_voiced_rms:
                voiced_streak += 1
                silence_streak = 0
                silence_clear_emitted = False
            else:
                silence_streak += 1
                voiced_streak = 0
            if not speech_active and voiced_streak >= vad_voiced_chunks:
                speech_active = True
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
            delta_norm = " ".join((delta or "").split()).strip()
            if delta_norm:
                sentence_buf = f"{sentence_buf} {delta_norm}".strip() if sentence_buf else delta_norm
                silence_clear_emitted = False
                if _SENTENCE_END_RE.search(delta_norm):
                    sentence = sentence_buf.strip()
                    sentence_buf = ""
                    speech_active = False
                    silence_streak = 0
                    if sentence:
                        # Backend-side near-duplicate suppression.
                        duplicate = any(_token_jaccard(sentence, prev) >= 0.92 for prev in recent_sentences)
                        if not duplicate:
                            recent_sentences.append(sentence)
                            sentence_seq += 1
                            await websocket.send_json({
                                "type": "sentence",
                                "sentence_id": sentence_seq,
                                "text": sentence,
                                "reason": "punctuation",
                                "t_unix": time.time(),
                            })
            elif sentence_buf and speech_active and silence_streak >= vad_silence_chunks:
                sentence = sentence_buf.strip()
                sentence_buf = ""
                speech_active = False
                silence_streak = 0
                if sentence:
                    duplicate = any(_token_jaccard(sentence, prev) >= 0.92 for prev in recent_sentences)
                    if not duplicate:
                        recent_sentences.append(sentence)
                        sentence_seq += 1
                        await websocket.send_json({
                            "type": "sentence",
                            "sentence_id": sentence_seq,
                            "text": sentence,
                            "reason": "vad_silence",
                            "t_unix": time.time(),
                        })
            elif sentence_buf and silence_streak >= hard_reset_chunks:
                # Long silence with unresolved buffer: finalize if meaningful, else hard reset.
                sentence = sentence_buf.strip()
                sentence_buf = ""
                last_window_transcript = ""
                speech_active = False
                voiced_streak = 0
                silence_streak = 0
                if sentence and len(sentence) >= 8:
                    duplicate = any(_token_jaccard(sentence, prev) >= 0.92 for prev in recent_sentences)
                    if not duplicate:
                        recent_sentences.append(sentence)
                        sentence_seq += 1
                        await websocket.send_json({
                            "type": "sentence",
                            "sentence_id": sentence_seq,
                            "text": sentence,
                            "reason": "hard_reset_finalize",
                            "t_unix": time.time(),
                        })
                if not silence_clear_emitted:
                    silence_clear_emitted = True
                    await websocket.send_json({
                        "type": "silence_clear",
                        "reason": "hard_reset",
                        "t_unix": time.time(),
                    })
            elif not sentence_buf and not speech_active and silence_streak >= silence_clear_chunks and not silence_clear_emitted:
                silence_clear_emitted = True
                await websocket.send_json({
                    "type": "silence_clear",
                    "reason": "long_silence",
                    "silence_chunks": silence_streak,
                    "t_unix": time.time(),
                })
            await websocket.send_json({
                "type": "partial",
                "text": text,
                "delta": delta,
                "window_sec": window_sec,
                "t_unix": time.time(),
            })
    except WebSocketDisconnect:
        return
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
