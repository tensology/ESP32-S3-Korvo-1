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
) -> None:
    """Synthesize + stream to ESP32 without blocking the HTTP response (realtime pacing can take many seconds)."""
    t0 = time.monotonic()
    n_streamed = 0
    try:
        pieces = _split_text_for_streaming_tts(translated) if tts_sentence_stream else [translated]
        for piece in pieces:
            p = (piece or "").strip()
            if not p:
                continue
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
            await stream_wav_bytes_to_board(
                board_base,
                wav_bytes,
                asyncio.Event(),
                stream_volume=clamp_stream_volume(stream_volume),
            )
            try:
                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                    frames = int(wf.getnframes())
                    rate = int(wf.getframerate()) or 16000
                    dur_est = max(0.05, frames / float(rate))
            except Exception:
                dur_est = max(0.2, len(wav_bytes) / 32000.0)
            # Short tail: resume ASR sooner after TTS (full dur_est still covers playback).
            suppress_for(dur_est + 0.35, key=_asr_suppress_key(board_base, stream_key))
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


router = APIRouter(prefix="/api", tags=["translation"])
logger = logging.getLogger("korvo.translation")


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
    if len(t) <= 120:
        return [t]
    parts = re.split(r"(?<=[。.!?！？…])\s+|\n+", t)
    chunks = [p.strip() for p in parts if p.strip()]
    if len(chunks) <= 1:
        return [t]
    return chunks


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
    playback_target: str = "server_local"
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

    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": source,
        "tl": target,
        "dt": "t",
        "q": text,
    }
    try:
        # Translate only — keep generous; slow networks / Google stalls should not kill the request at 20s.
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

    with kdb.lock:
        rows = kdb.conn.execute("SELECT key, value FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    aws_polly_enabled = settings.get("aws_polly_enabled", "0") == "1"

    speak_done = False
    speak_pending = False
    speak_error = ""
    tts_board_chunks = 0
    if body.speak_target:
        if body.kokoro_voice not in KOKORO_VOICES:
            raise HTTPException(400, "Invalid Kokoro voice")
        if body.kokoro_speed <= 0:
            raise HTTPException(400, "Kokoro speed must be positive")
        if body.playback_target not in {"server_local", "board_inject"}:
            raise HTTPException(400, "playback_target must be server_local or board_inject")
        voice_used = body.kokoro_voice
        expected_prefix = kokoro_lang.split("-")[0][0] if kokoro_lang else ""
        # Guard against mismatched language/voice (e.g. af_* for ja) that causes gibberish.
        if expected_prefix and not body.kokoro_voice.startswith(expected_prefix):
            voice_used = KOKORO_DEFAULT_VOICE_BY_LANG.get(kokoro_lang, body.kokoro_voice)
        try:
            # Always speak the translated target text in the selected target language.
            if body.playback_target == "server_local":
                asyncio.create_task(
                    _server_local_tts_background(
                        translated=translated,
                        kokoro_lang=kokoro_lang,
                        voice_used=voice_used,
                        aws_polly_enabled=aws_polly_enabled,
                        settings=settings,
                        kokoro_speed=body.kokoro_speed,
                        stream_key=(body.stream_key or "").strip(),
                    )
                )
                speak_done = False
                speak_pending = True
            else:
                board_base = normalize_board_base(body.board_url)
                if not board_base:
                    raise HTTPException(400, "board_url is required for board_inject playback_target")
                if not _allowed_board_url(board_base):
                    raise HTTPException(400, "board_url host not allowed")
                pieces = (
                    _split_text_for_streaming_tts(translated)
                    if body.tts_sentence_stream
                    else [translated]
                )
                tts_board_chunks = sum(1 for x in pieces if (x or "").strip())
                asyncio.create_task(
                    _board_inject_tts_background(
                        board_base=board_base,
                        translated=translated,
                        kokoro_lang=kokoro_lang,
                        voice_used=voice_used,
                        aws_polly_enabled=aws_polly_enabled,
                        settings=settings,
                        tts_sentence_stream=body.tts_sentence_stream,
                        kokoro_speed=body.kokoro_speed,
                        stream_key=body.stream_key,
                        stream_volume=body.stream_volume,
                    )
                )
                speak_done = False
                speak_pending = True
        except Exception as e:  # noqa: BLE001
            speak_error = str(e)
    else:
        voice_used = body.kokoro_voice

    # Debug comparison logging: what was translated vs what was sent to TTS.
    tts_input_text = translated if body.speak_target else ""
    logger.info(
        "translate_click source_lang=%s target_lang=%s speak_target=%s voice=%s kokoro_lang=%s source_text=%r translated_text=%r tts_input_text=%r speak_done=%s speak_pending=%s speak_error=%r",
        source,
        target,
        body.speak_target,
        body.kokoro_voice,
        kokoro_lang,
        text,
        translated,
        tts_input_text,
        speak_done,
        speak_pending,
        speak_error,
    )
    _write_tts_compare_log(
        {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "source_language": source,
            "target_language": target,
            "speak_target": body.speak_target,
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
        }
    )

    return {
        "translated_text": translated,
        "source_language": source,
        "target_language": target,
        "speak_target": body.speak_target,
        "playback_target": body.playback_target,
        "speak_done": speak_done,
        "speak_pending": speak_pending,
        "speak_error": speak_error,
        "tts_board_chunks": tts_board_chunks,
        "kokoro_lang": kokoro_lang,
        "tts_voice_used": voice_used,
        "tts_input_text": tts_input_text,
        "tts_vendor_used": "aws_polly" if aws_polly_enabled and body.speak_target else "kokoro",
    }
