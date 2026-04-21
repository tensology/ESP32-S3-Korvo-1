import httpx
import logging
import json
import os
import shutil
import subprocess
import tempfile
import threading
import wave
import io
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from korvo_server.config import LOGS_DIR
from korvo_server.deps import KorvoDep
from korvo_server.routers.api_tts import KOKORO_VOICES, _synthesize, synthesize_polly
from korvo_server.tts_echo_guard import suppress_for

router = APIRouter(prefix="/api", tags=["translation"])
logger = logging.getLogger("korvo.translation")
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
        suppress_for(duration_s + 0.8, key=(stream_key or None))
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


SUPPORTED_TTS_TARGETS = {"en", "ja", "es", "fr", "it", "pt", "hi", "zh-cn", "zh-tw"}


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
        async with httpx.AsyncClient(timeout=20.0) as client:
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
    speak_error = ""
    if body.speak_target:
        if body.kokoro_voice not in KOKORO_VOICES:
            raise HTTPException(400, "Invalid Kokoro voice")
        if body.kokoro_speed <= 0:
            raise HTTPException(400, "Kokoro speed must be positive")
        if body.playback_target != "server_local":
            raise HTTPException(400, "Only server_local playback_target is supported")
        voice_used = body.kokoro_voice
        expected_prefix = kokoro_lang.split("-")[0][0] if kokoro_lang else ""
        # Guard against mismatched language/voice (e.g. af_* for ja) that causes gibberish.
        if expected_prefix and not body.kokoro_voice.startswith(expected_prefix):
            voice_used = KOKORO_DEFAULT_VOICE_BY_LANG.get(kokoro_lang, body.kokoro_voice)
        try:
            # Always speak the translated target text in the selected target language.
            if aws_polly_enabled:
                wav_bytes = synthesize_polly(
                    translated,
                    kokoro_lang,
                    settings.get("aws_access_key_id", ""),
                    settings.get("aws_secret_access_key", ""),
                    settings.get("aws_region", "eu-west-1"),
                    settings.get("aws_session_token", ""),
                )
            else:
                wav_bytes = _synthesize(translated, voice_used, kokoro_lang, body.kokoro_speed)
            _play_local_wav(wav_bytes, body.stream_key.strip())
            speak_done = True
        except Exception as e:  # noqa: BLE001
            speak_error = str(e)
    else:
        voice_used = body.kokoro_voice

    # Debug comparison logging: what was translated vs what was sent to TTS.
    tts_input_text = translated if body.speak_target else ""
    logger.info(
        "translate_click source_lang=%s target_lang=%s speak_target=%s voice=%s kokoro_lang=%s source_text=%r translated_text=%r tts_input_text=%r speak_done=%s speak_error=%r",
        source,
        target,
        body.speak_target,
        body.kokoro_voice,
        kokoro_lang,
        text,
        translated,
        tts_input_text,
        speak_done,
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
        "speak_done": speak_done,
        "speak_error": speak_error,
        "kokoro_lang": kokoro_lang,
        "tts_voice_used": voice_used,
        "tts_input_text": tts_input_text,
        "tts_vendor_used": "aws_polly" if aws_polly_enabled and body.speak_target else "kokoro",
    }
