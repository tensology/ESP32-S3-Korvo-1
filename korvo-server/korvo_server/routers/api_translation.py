import httpx
import logging
import json
import os
import re
import time
import asyncio
import shutil
import subprocess
import tempfile
import threading
import wave
import io
from dataclasses import dataclass, field
from urllib.parse import urlparse
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from korvo_server.audio_push import clamp_stream_volume, normalize_board_base, stream_wav_bytes_to_board
from korvo_server.config import LOGS_DIR
from korvo_server.deps import KorvoDep
from korvo_server.routers.api_tts import KOKORO_VOICES, _synthesize, synthesize_polly
from korvo_server.tts_echo_guard import suppress_for

# Must match the key used by /ws/audio/transcribe (board stream URL) for echo suppression.
def _asr_suppress_key(board_base: str, stream_key: str) -> str:
    sk = (stream_key or "").strip()
    if sk:
        return sk
    b = (board_base or "").strip().rstrip("/")
    return f"{b}/api/audio/stream" if b else ""


async def _server_local_tts_background(
    *,
    translated: str,
    kokoro_lang: str,
    voice_used: str,
    aws_polly_enabled: bool,
    settings: dict,
    kokoro_speed: float,
    stream_key: str,
) -> None:
    """Synthesize + afplay without blocking the HTTP response (Kokoro can take many seconds)."""
    t0 = time.monotonic()
    try:
        if aws_polly_enabled:
            wav_bytes = await asyncio.to_thread(
                synthesize_polly,
                translated,
                kokoro_lang,
                settings.get("aws_access_key_id", ""),
                settings.get("aws_secret_access_key", ""),
                settings.get("aws_region", "eu-west-1"),
                settings.get("aws_session_token", ""),
            )
        else:
            wav_bytes = await asyncio.to_thread(
                _synthesize,
                translated,
                voice_used,
                kokoro_lang,
                kokoro_speed,
            )
        await asyncio.to_thread(_play_local_wav, wav_bytes, stream_key)
        elapsed = time.monotonic() - t0
        logger.info(
            "server_local_tts_finished ok=True sec=%.2f translated_len=%d",
            elapsed,
            len(translated or ""),
        )
        _append_tts_background_event(
            "server_local_done",
            ok=True,
            elapsed_sec=round(elapsed, 3),
            translated_len=len(translated or ""),
        )
    except Exception as e:
        logger.exception("server_local TTS background task failed")
        _append_tts_background_event(
            "server_local_done",
            ok=False,
            elapsed_sec=round(time.monotonic() - t0, 3),
            error=str(e)[:500],
        )


async def _board_inject_tts_background(
    *,
    board_base: str,
    translated: str,
    kokoro_lang: str,
    voice_used: str,
    aws_polly_enabled: bool,
    settings: dict,
    tts_sentence_stream: bool,
    kokoro_speed: float,
    stream_key: str,
    stream_volume: float,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Synthesize + stream to ESP32 without blocking the HTTP response (realtime pacing can take many seconds)."""
    t0 = time.monotonic()
    n_streamed = 0
    abort_event = stop_event or asyncio.Event()
    try:
        pieces = _split_text_for_streaming_tts(translated) if tts_sentence_stream else [translated]
        non_empty_pieces = [x for x in pieces if (x or "").strip()]
        total_pieces = len(non_empty_pieces)
        piece_idx = 0
        for piece in pieces:
            if abort_event.is_set():
                logger.info("board_inject_tts preempted before piece start board=%s", board_base)
                break
            p = (piece or "").strip()
            if not p:
                continue
            piece_idx += 1
            synth_t0 = time.monotonic()
            if aws_polly_enabled:
                wav_bytes = await asyncio.to_thread(
                    synthesize_polly,
                    p,
                    kokoro_lang,
                    settings.get("aws_access_key_id", ""),
                    settings.get("aws_secret_access_key", ""),
                    settings.get("aws_region", "eu-west-1"),
                    settings.get("aws_session_token", ""),
                )
            else:
                wav_bytes = await asyncio.to_thread(
                    _synthesize,
                    p,
                    voice_used,
                    kokoro_lang,
                    kokoro_speed,
                )
            synth_sec = time.monotonic() - synth_t0
            inject_t0 = time.monotonic()
            await stream_wav_bytes_to_board(
                board_base,
                wav_bytes,
                abort_event,
                stream_volume=clamp_stream_volume(stream_volume),
            )
            inject_sec = time.monotonic() - inject_t0
            if abort_event.is_set():
                logger.info("board_inject_tts preempted after piece board=%s", board_base)
                break
            logger.info(
                "board_inject_piece board=%s idx=%d/%d chars=%d synth_sec=%.3f inject_sec=%.3f",
                board_base,
                piece_idx,
                total_pieces,
                len(p),
                synth_sec,
                inject_sec,
            )
            _append_tts_background_event(
                "board_inject_piece",
                board_base=board_base,
                piece_index=piece_idx,
                total_pieces=total_pieces,
                piece_chars=len(p),
                synth_sec=round(synth_sec, 3),
                inject_sec=round(inject_sec, 3),
            )
            try:
                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                    frames = int(wf.getnframes())
                    rate = int(wf.getframerate()) or 16000
                    dur_est = max(0.05, frames / float(rate))
            except Exception:
                dur_est = max(0.2, len(wav_bytes) / 32000.0)
            # Keep echo guard conservative but bounded. Under rapid turn-taking,
            # uncapped piece-by-piece extension can make ASR appear "stuck off".
            suppress_for(
                dur_est + 0.35,
                key=_asr_suppress_key(board_base, stream_key),
                max_horizon_sec=1.8,
            )
            n_streamed += 1
        elapsed = time.monotonic() - t0
        logger.info(
            "board_inject_tts_finished ok=True board=%s sec=%.2f streamed_pieces=%d translated_len=%d",
            board_base,
            elapsed,
            n_streamed,
            len(translated or ""),
        )
        _append_tts_background_event(
            "board_inject_done",
            ok=True,
            board_base=board_base,
            elapsed_sec=round(elapsed, 3),
            streamed_pieces=n_streamed,
            translated_len=len(translated or ""),
            preempted=bool(abort_event.is_set()),
        )
    except Exception as e:
        logger.exception("board_inject TTS background task failed")
        _append_tts_background_event(
            "board_inject_done",
            ok=False,
            board_base=board_base,
            elapsed_sec=round(time.monotonic() - t0, 3),
            error=str(e)[:500],
        )
        # Propagate failure to the session worker so counters/status reflect reality.
        raise


async def _flush_board_inject_queue(board_base: str) -> None:
    """
    Best-effort queue flush on the board so new utterances do not wait behind
    stale buffered audio after preemption.
    """
    b = normalize_board_base(board_base)
    if not b:
        return
    url = f"{b}/api/audio/inject/flush"
    timeout = httpx.Timeout(connect=1.5, read=2.0, write=2.0, pool=None)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers={"Content-Type": "application/json"}, content=b"{}")
            # Older firmware may not expose this endpoint yet.
            if r.status_code in (404, 405, 501):
                return
    except Exception:
        return


@dataclass
class _PipelineSession:
    session_key: str
    board_base: str
    translate_queue: asyncio.Queue[dict] = field(default_factory=lambda: asyncio.Queue(maxsize=2))
    tts_queue: asyncio.Queue[dict] = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    translate_task: asyncio.Task | None = None
    tts_task: asyncio.Task | None = None
    active_tts_stop_event: asyncio.Event | None = None
    submitted_total: int = 0
    translate_completed_total: int = 0
    translate_failed_total: int = 0
    tts_enqueued_total: int = 0
    tts_replaced_total: int = 0
    tts_completed_total: int = 0
    tts_failed_total: int = 0
    tts_preempted_total: int = 0
    last_error: str = ""
    updated_at: float = field(default_factory=time.monotonic)


async def _translate_text_google(text: str, source: str, target: str) -> tuple[str, int]:
    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text}
    translate_started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Google Translate HTTP error: {e.response.status_code}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Translation request failed: {e}") from e
    translated = ""
    try:
        parts = data[0] or []
        translated = "".join((p[0] or "") for p in parts if isinstance(p, list) and p)
    except Exception:  # noqa: BLE001
        translated = ""
    if not translated:
        raise HTTPException(502, "No translation returned")
    translate_ms = int(round((time.monotonic() - translate_started) * 1000.0))
    return translated, translate_ms


async def _run_translate_stage(session: _PipelineSession) -> None:
    while True:
        job = await session.translate_queue.get()
        session.updated_at = time.monotonic()
        fut: asyncio.Future = job["result_future"]
        try:
            translated, translate_ms = await _translate_text_google(
                job["text"],
                job["source"],
                job["target"],
            )
            if not fut.done():
                fut.set_result((translated, translate_ms))
            session.translate_completed_total += 1

            if job["speak_target"]:
                should_flush = False
                tts_payload = {
                    "playback_target": job["playback_target"],
                    "board_base": job["board_base"],
                    "translated": translated,
                    "kokoro_lang": job["kokoro_lang"],
                    "voice_used": job["voice_used"],
                    "aws_polly_enabled": job["aws_polly_enabled"],
                    "settings": job["settings"],
                    "tts_sentence_stream": job["tts_sentence_stream"],
                    "kokoro_speed": job["kokoro_speed"],
                    "stream_key": job["stream_key"],
                    "stream_volume": job["stream_volume"],
                    "flush_before_play": False,
                }
                if session.active_tts_stop_event is not None:
                    session.active_tts_stop_event.set()
                    session.tts_preempted_total += 1
                    should_flush = True
                if session.tts_queue.full():
                    try:
                        _ = session.tts_queue.get_nowait()
                        session.tts_queue.task_done()
                        session.tts_replaced_total += 1
                        should_flush = True
                    except asyncio.QueueEmpty:
                        pass
                tts_payload["flush_before_play"] = should_flush
                await session.tts_queue.put(tts_payload)
                session.tts_enqueued_total += 1
        except Exception as e:  # noqa: BLE001
            session.translate_failed_total += 1
            session.last_error = str(e)[:500]
            if not fut.done():
                fut.set_exception(e)
        finally:
            session.updated_at = time.monotonic()
            session.translate_queue.task_done()


async def _run_tts_stage(session: _PipelineSession) -> None:
    while True:
        payload = await session.tts_queue.get()
        session.updated_at = time.monotonic()
        stop_event = asyncio.Event()
        session.active_tts_stop_event = stop_event
        try:
            if payload["playback_target"] == "server_local":
                await _server_local_tts_background(
                    translated=payload["translated"],
                    kokoro_lang=payload["kokoro_lang"],
                    voice_used=payload["voice_used"],
                    aws_polly_enabled=payload["aws_polly_enabled"],
                    settings=payload["settings"],
                    kokoro_speed=payload["kokoro_speed"],
                    stream_key=payload["stream_key"],
                )
            else:
                if payload.get("flush_before_play"):
                    await _flush_board_inject_queue(payload["board_base"])
                await _board_inject_tts_background(
                    board_base=payload["board_base"],
                    translated=payload["translated"],
                    kokoro_lang=payload["kokoro_lang"],
                    voice_used=payload["voice_used"],
                    aws_polly_enabled=payload["aws_polly_enabled"],
                    settings=payload["settings"],
                    tts_sentence_stream=payload["tts_sentence_stream"],
                    kokoro_speed=payload["kokoro_speed"],
                    stream_key=payload["stream_key"],
                    stream_volume=payload["stream_volume"],
                    stop_event=stop_event,
                )
            session.tts_completed_total += 1
        except Exception as e:  # noqa: BLE001
            session.tts_failed_total += 1
            session.last_error = str(e)[:500]
            logger.exception("tts stage failed session=%s", session.session_key)
        finally:
            session.active_tts_stop_event = None
            session.updated_at = time.monotonic()
            session.tts_queue.task_done()


def _session_snapshot(session: _PipelineSession) -> dict:
    return {
        "session_key": session.session_key,
        "board_base": session.board_base,
        "translate_queue_depth": session.translate_queue.qsize(),
        "tts_queue_depth": session.tts_queue.qsize(),
        # Backward-compatible fields currently consumed by dashboard status text.
        "queue_depth": session.tts_queue.qsize(),
        "enqueued_total": session.tts_enqueued_total,
        "replaced_total": session.tts_replaced_total,
        "completed_total": session.tts_completed_total,
        "failed_total": session.tts_failed_total,
        "preempted_total": session.tts_preempted_total,
        "submitted_total": session.submitted_total,
        "translate_completed_total": session.translate_completed_total,
        "translate_failed_total": session.translate_failed_total,
        "tts_enqueued_total": session.tts_enqueued_total,
        "tts_replaced_total": session.tts_replaced_total,
        "tts_completed_total": session.tts_completed_total,
        "tts_failed_total": session.tts_failed_total,
        "tts_preempted_total": session.tts_preempted_total,
        "last_error": session.last_error,
    }


async def _submit_pipeline_job(
    *,
    text: str,
    source: str,
    target: str,
    speak_target: bool,
    playback_target: str,
    board_base: str,
    kokoro_lang: str,
    voice_used: str,
    aws_polly_enabled: bool,
    settings: dict,
    tts_sentence_stream: bool,
    kokoro_speed: float,
    stream_key: str,
    stream_volume: float,
) -> tuple[str, int, dict]:
    session_key = board_base if (speak_target and playback_target == "board_inject" and board_base) else playback_target
    if not session_key:
        session_key = "translate_only"
    fut: asyncio.Future = asyncio.get_running_loop().create_future()

    async with _pipeline_state_lock:
        session = _pipeline_sessions.get(session_key)
        if session is None:
            session = _PipelineSession(session_key=session_key, board_base=board_base)
            _pipeline_sessions[session_key] = session
        session.board_base = board_base
        if session.translate_task is None or session.translate_task.done():
            session.translate_task = asyncio.create_task(_run_translate_stage(session))
        if session.tts_task is None or session.tts_task.done():
            session.tts_task = asyncio.create_task(_run_tts_stage(session))

        if session.translate_queue.full():
            try:
                dropped = session.translate_queue.get_nowait()
                session.translate_queue.task_done()
                dropped_future = dropped.get("result_future")
                if isinstance(dropped_future, asyncio.Future) and not dropped_future.done():
                    dropped_future.set_exception(RuntimeError("dropped_due_to_backpressure"))
            except asyncio.QueueEmpty:
                pass

        payload = {
            "text": text,
            "source": source,
            "target": target,
            "speak_target": speak_target,
            "playback_target": playback_target,
            "board_base": board_base,
            "kokoro_lang": kokoro_lang,
            "voice_used": voice_used,
            "aws_polly_enabled": aws_polly_enabled,
            "settings": settings,
            "tts_sentence_stream": tts_sentence_stream,
            "kokoro_speed": kokoro_speed,
            "stream_key": stream_key,
            "stream_volume": stream_volume,
            "result_future": fut,
        }
        await session.translate_queue.put(payload)
        session.submitted_total += 1
        session.updated_at = time.monotonic()
        snapshot = _session_snapshot(session)

    translated, translate_ms = await asyncio.wait_for(fut, timeout=70.0)
    return translated, translate_ms, snapshot


router = APIRouter(prefix="/api", tags=["translation"])
logger = logging.getLogger("korvo.translation")
_pipeline_state_lock = asyncio.Lock()
_pipeline_sessions: dict[str, _PipelineSession] = {}


def _append_tts_background_event(kind: str, **fields: object) -> None:
    """Append one JSON line so logs show when background TTS finished (request log alone is not enough)."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        out = LOGS_DIR / "translation_tts_compare.jsonl"
        row: dict = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "event": kind,
            **{k: v for k, v in fields.items() if v is not None},
        }
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
_local_play_lock = threading.Lock()
_local_play_proc = None

KOKORO_DEFAULT_VOICE_BY_LANG = {
    "en-us": "af_heart",
    "en-gb": "bf_emma",
    "es": "ef_dora",
    "fr-fr": "ff_siwis",
    "hi": "hf_alpha",
    "it": "if_sara",
    "ja": "jf_alpha",
    "pt-br": "pf_dora",
    "zh": "zf_xiaobei",
}


def _split_text_for_streaming_tts(text: str) -> list[str]:
    """Split on sentence-like boundaries; synthesize+play chunks in order for faster time-to-first-audio on the board."""
    t = (text or "").strip()
    if not t:
        return []
    # Keep chunks small so first audio starts quickly even for medium phrases.
    # This is pseudo-streaming for non-streaming TTS backends.
    if len(t) <= 42:
        return [t]
    parts = re.split(r"(?<=[。.!?！？…])\s+|\n+|(?<=[,，、;；:：])\s*", t)
    chunks = [p.strip() for p in parts if p.strip()]
    if len(chunks) <= 1:
        return [t]
    merged: list[str] = []
    cur = ""
    for c in chunks:
        if not cur:
            cur = c
            continue
        if len(cur) + 1 + len(c) <= 34:
            cur = f"{cur} {c}"
            continue
        merged.append(cur)
        cur = c
    if cur:
        merged.append(cur)
    return merged or [t]


def _write_tts_compare_log(payload: dict) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        out = LOGS_DIR / "translation_tts_compare.jsonl"
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # Never break user flow due to logging failure.
        pass


def _play_local_wav(wav_bytes: bytes, stream_key: str = "") -> None:
    global _local_play_proc
    if os.name != "posix":
        raise RuntimeError("Local playback is supported on macOS/Linux hosts only")
    if not shutil.which("afplay"):
        raise RuntimeError("afplay not found on server host")
    with _local_play_lock:
        # Stop previous playback so short phrases do not overlap/repeat.
        if _local_play_proc is not None:
            try:
                if _local_play_proc.poll() is None:
                    _local_play_proc.terminate()
            except Exception:
                pass
        with tempfile.NamedTemporaryFile(prefix="korvo-kokoro-", suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            wav_path = f.name
        # Guard ASR from retranscribing local TTS playback (speaker echo loop).
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                frames = int(wf.getnframes())
                rate = int(wf.getframerate()) or 16000
                duration_s = max(0.0, frames / float(rate))
        except Exception:
            duration_s = 1.0
        suppress_for(duration_s + 0.35, key=(stream_key or None))
        _local_play_proc = subprocess.Popen(["afplay", wav_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TranslateBody(BaseModel):
    text: str
    source_language: str = "en"
    target_language: str = "ja"
    speak_target: bool = False
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = 1.0
    playback_target: str = "board_inject"
    stream_key: str = ""
    board_url: str = ""
    stream_volume: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Linear gain for board inject TTS (0–1). Ignored for server_local.",
    )
    tts_sentence_stream: bool = Field(
        True,
        description="For board TTS: split long translations on sentence boundaries and stream each chunk in order (earlier first sound).",
    )


SUPPORTED_TTS_TARGETS = {"en", "ja", "es", "fr", "it", "pt", "hi", "zh-cn", "zh-tw"}


def _allowed_board_url(url: str) -> bool:
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
    if hn.startswith("192.168.") or hn.startswith("10.") or hn.startswith("172."):
        return True
    return False


@router.post("/translate/google")
async def translate_google(body: TranslateBody, kdb: KorvoDep):
    req_started = time.monotonic()
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Text is required")

    source = (body.source_language or "en").strip().lower() or "en"
    target = (body.target_language or "ja").strip().lower() or "ja"
    if len(source) > 12 or len(target) > 12:
        raise HTTPException(400, "Invalid language code")
    if source == target:
        raise HTTPException(400, "Source and target languages must be different")
    if target not in SUPPORTED_TTS_TARGETS:
        raise HTTPException(400, f"Target language '{target}' is not supported by Kokoro TTS")

    kokoro_lang_map = {
        "en": "en-us",
        "ja": "ja",
        "es": "es",
        "fr": "fr-fr",
        "de": "de",
        "it": "it",
        "pt": "pt-br",
        "ko": "ko",
        "hi": "hi",
        "ru": "ru",
        "ar": "ar",
        "zh-cn": "zh",
        "zh-tw": "zh",
    }
    kokoro_lang = kokoro_lang_map.get(target, "en-us")

    with kdb.lock:
        rows = kdb.conn.execute("SELECT key, value FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    aws_polly_enabled = settings.get("aws_polly_enabled", "0") == "1"
    playback_target = (body.playback_target or "board_inject").strip().lower() or "board_inject"
    if playback_target not in {"server_local", "board_inject"}:
        logger.warning("Invalid playback_target '%s'; defaulting to board_inject", body.playback_target)
        playback_target = "board_inject"

    speak_done = False
    speak_pending = False
    speak_error = ""
    tts_board_chunks = 0
    tts_orchestrator: dict = {}
    if body.kokoro_voice not in KOKORO_VOICES:
        raise HTTPException(400, "Invalid Kokoro voice")
    if body.kokoro_speed <= 0:
        raise HTTPException(400, "Kokoro speed must be positive")
    voice_used = body.kokoro_voice
    expected_prefix = kokoro_lang.split("-")[0][0] if kokoro_lang else ""
    # Guard against mismatched language/voice (e.g. af_* for ja) that causes gibberish.
    if expected_prefix and not body.kokoro_voice.startswith(expected_prefix):
        voice_used = KOKORO_DEFAULT_VOICE_BY_LANG.get(kokoro_lang, body.kokoro_voice)

    board_base = ""
    if body.speak_target and playback_target == "board_inject":
        board_base = normalize_board_base(body.board_url)
        if not board_base:
            raise HTTPException(400, "board_url is required for board_inject playback_target")
        if not _allowed_board_url(board_base):
            raise HTTPException(400, "board_url host not allowed")

    try:
        translated, translate_ms, tts_orchestrator = await _submit_pipeline_job(
            text=text,
            source=source,
            target=target,
            speak_target=bool(body.speak_target),
            playback_target=playback_target,
            board_base=board_base,
            kokoro_lang=kokoro_lang,
            voice_used=voice_used,
            aws_polly_enabled=aws_polly_enabled,
            settings=settings,
            tts_sentence_stream=bool(body.tts_sentence_stream),
            kokoro_speed=body.kokoro_speed,
            stream_key=(body.stream_key or "").strip(),
            stream_volume=body.stream_volume,
        )
    except TimeoutError as e:
        raise HTTPException(504, "Translation pipeline timeout") from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Translation pipeline failed: {e}") from e

    if body.speak_target:
        speak_pending = True
        if playback_target == "board_inject":
            pieces = _split_text_for_streaming_tts(translated) if body.tts_sentence_stream else [translated]
            tts_board_chunks = sum(1 for x in pieces if (x or "").strip())

    # Debug comparison logging: what was translated vs what was sent to TTS.
    tts_input_text = translated if body.speak_target else ""
    logger.info(
        "translate_click source_lang=%s target_lang=%s speak_target=%s playback_target=%s voice=%s kokoro_lang=%s source_text=%r translated_text=%r tts_input_text=%r speak_done=%s speak_pending=%s speak_error=%r translate_ms=%d total_ms=%d",
        source,
        target,
        body.speak_target,
        playback_target,
        body.kokoro_voice,
        kokoro_lang,
        text,
        translated,
        tts_input_text,
        speak_done,
        speak_pending,
        speak_error,
        translate_ms,
        int(round((time.monotonic() - req_started) * 1000.0)),
    )
    _write_tts_compare_log(
        {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "source_language": source,
            "target_language": target,
            "speak_target": body.speak_target,
            "playback_target": playback_target,
            "kokoro_voice": body.kokoro_voice,
            "tts_voice_used": voice_used,
            "kokoro_lang": kokoro_lang,
            "source_text": text,
            "translated_text": translated,
            "tts_input_text": tts_input_text,
            "speak_done": speak_done,
            "speak_pending": speak_pending,
            "speak_error": speak_error,
            "source_len": len(text),
            "translated_len": len(translated),
            "tts_input_len": len(tts_input_text),
            "tts_orchestrator": tts_orchestrator,
            "translate_ms": translate_ms,
            "total_ms": int(round((time.monotonic() - req_started) * 1000.0)),
        }
    )

    return {
        "translated_text": translated,
        "source_language": source,
        "target_language": target,
        "speak_target": body.speak_target,
        "playback_target": playback_target,
        "speak_done": speak_done,
        "speak_pending": speak_pending,
        "speak_error": speak_error,
        "tts_board_chunks": tts_board_chunks,
        "kokoro_lang": kokoro_lang,
        "tts_voice_used": voice_used,
        "tts_input_text": tts_input_text,
        "tts_vendor_used": "aws_polly" if aws_polly_enabled and body.speak_target else "kokoro",
        "tts_orchestrator": tts_orchestrator,
        "translate_ms": translate_ms,
        "total_ms": int(round((time.monotonic() - req_started) * 1000.0)),
    }


@router.get("/translation/tts-sessions")
async def translation_tts_sessions():
    async with _pipeline_state_lock:
        rows = []
        now = time.monotonic()
        for _, session in _pipeline_sessions.items():
            rows.append(
                {
                    "session_key": session.session_key,
                    "board_base": session.board_base,
                    "translate_queue_depth": session.translate_queue.qsize(),
                    "tts_queue_depth": session.tts_queue.qsize(),
                    # Backward-compatible fields for existing dashboard polling.
                    "queue_depth": session.tts_queue.qsize(),
                    "enqueued_total": session.tts_enqueued_total,
                    "replaced_total": session.tts_replaced_total,
                    "completed_total": session.tts_completed_total,
                    "failed_total": session.tts_failed_total,
                    "preempted_total": session.tts_preempted_total,
                    "translate_worker_alive": bool(session.translate_task and not session.translate_task.done()),
                    "tts_worker_alive": bool(session.tts_task and not session.tts_task.done()),
                    "active": bool(session.active_tts_stop_event is not None),
                    "submitted_total": session.submitted_total,
                    "translate_completed_total": session.translate_completed_total,
                    "translate_failed_total": session.translate_failed_total,
                    "tts_enqueued_total": session.tts_enqueued_total,
                    "tts_replaced_total": session.tts_replaced_total,
                    "tts_completed_total": session.tts_completed_total,
                    "tts_failed_total": session.tts_failed_total,
                    "tts_preempted_total": session.tts_preempted_total,
                    "last_error": session.last_error,
                    "idle_sec": round(max(0.0, now - session.updated_at), 3),
                }
            )
    return {"sessions": rows}
