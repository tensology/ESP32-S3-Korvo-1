from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import wave
from functools import lru_cache
from urllib.parse import unquote

import boto3
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from korvo_server.config import KOKORO_DIR

router = APIRouter(prefix="/api/tts", tags=["tts"])

# Common Kokoro voice IDs (keep explicit and stable for UI).
KOKORO_VOICES = [
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
    "ef_dora",
    "em_alex",
    "em_santa",
    "ff_siwis",
    "hf_alpha",
    "hf_beta",
    "hm_omega",
    "hm_psi",
    "if_sara",
    "im_nicola",
    "jf_alpha",
    "jf_gongitsune",
    "jf_nezumi",
    "jf_tebukuro",
    "jm_kumo",
    "pf_dora",
    "pm_alex",
    "pm_santa",
    "zf_xiaobei",
    "zf_xiaoni",
    "zf_xiaoxiao",
    "zf_xiaoyi",
    "zm_yunjian",
    "zm_yunxi",
    "zm_yunxia",
    "zm_yunyang",
]

KOKORO_MODEL_URLS = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/latest/download/kokoro-v1.0.onnx",
    "https://sourceforge.net/projects/kokoro-onnx.mirror/files/model-files-v1.0/kokoro-v1.0.onnx/download",
)
KOKORO_VOICES_URLS = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/latest/download/voices-v1.0.bin",
    "https://sourceforge.net/projects/kokoro-onnx.mirror/files/model-files-v1.0/voices-v1.0.bin/download",
)


class KokoroSpeakBody(BaseModel):
    text: str
    voice: str = "af_heart"
    lang: str = "en-us"
    speed: float = 1.0


class PollyValidateBody(BaseModel):
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str = "eu-west-1"
    aws_session_token: str = ""


@lru_cache(maxsize=1)
def _kokoro():
    try:
        from kokoro_onnx import Kokoro  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("kokoro_onnx is not installed") from e

    model_path, voices_path = _ensure_kokoro_assets()
    return Kokoro(str(model_path), str(voices_path))


def _ensure_kokoro_assets():
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    model_path = KOKORO_DIR / "kokoro-v1.0.onnx"
    voices_path = KOKORO_DIR / "voices-v1.0.bin"

    def dl(urls, out_path):
        last_err = None
        for url in urls:
            try:
                with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as r:
                    r.raise_for_status()
                    with open(out_path, "wb") as f:
                        for chunk in r.iter_bytes():
                            if chunk:
                                f.write(chunk)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(f"Failed downloading Kokoro asset: {last_err}")

    if not model_path.is_file():
        dl(KOKORO_MODEL_URLS, model_path)
    if not voices_path.is_file():
        dl(KOKORO_VOICES_URLS, voices_path)
    if not model_path.is_file() or not voices_path.is_file():
        raise RuntimeError("Kokoro model assets are missing")
    return model_path, voices_path


def _to_wav_bytes(samples, sample_rate: int) -> bytes:
    pcm = bytearray()
    for s in samples:
        v = max(-1.0, min(1.0, float(s)))
        i = int(v * 32767.0)
        pcm.extend(i.to_bytes(2, byteorder="little", signed=True))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(pcm))
    return buf.getvalue()


@lru_cache(maxsize=16)
def _get_g2p(lang: str):
    code = (lang or "").strip().lower()
    if code == "ja":
        from misaki.ja import JAG2P  # type: ignore

        return JAG2P(version="pyopenjtalk")
    if code == "zh":
        from misaki.zh import ZHG2P  # type: ignore

        return ZHG2P(version="1.1")
    if code in {"es", "fr-fr", "it", "pt-br", "hi"}:
        from misaki.espeak import EspeakG2P  # type: ignore

        return EspeakG2P(code)
    return None


def _phonemize(text: str, lang: str) -> str:
    g2p = _get_g2p(lang)
    if g2p is None:
        return ""
    out = g2p(text)
    # misaki callables return (phonemes, tokens/None)
    if isinstance(out, tuple) and out:
        return str(out[0] or "").strip()
    return str(out or "").strip()


def _synthesize(text: str, voice: str, lang: str, speed: float) -> bytes:
    engine = _kokoro()
    phonemes = _phonemize(text, lang)
    if phonemes:
        samples, sample_rate = engine.create(
            phonemes,
            voice=voice,
            lang=lang,
            speed=speed,
            is_phonemes=True,
        )
    else:
        samples, sample_rate = engine.create(
            text,
            voice=voice,
            lang=lang,
            speed=speed,
        )
    return _to_wav_bytes(samples, int(sample_rate))


@router.get("/kokoro/voices")
def list_kokoro_voices():
    return {"voices": KOKORO_VOICES, "default": "af_heart"}


@router.post("/kokoro/speak")
def kokoro_speak(body: KokoroSpeakBody):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Text is required")
    if body.voice not in KOKORO_VOICES:
        raise HTTPException(400, "Invalid Kokoro voice")
    if body.speed <= 0:
        raise HTTPException(400, "Speed must be positive")

    try:
        wav_bytes = _synthesize(text, body.voice, body.lang, body.speed)
    except RuntimeError as e:
        raise HTTPException(503, f"{e}. Run korvo-server/setup.sh to install/download Kokoro.") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Kokoro synthesis failed: {e}") from e

    return StreamingResponse(io.BytesIO(wav_bytes), media_type="audio/wav")


@router.get("/kokoro/stream")
def kokoro_stream(text: str, voice: str = "af_heart", lang: str = "en-us", speed: float = 1.0):
    t = unquote(text or "").strip()
    if not t:
        raise HTTPException(400, "Text is required")
    if voice not in KOKORO_VOICES:
        raise HTTPException(400, "Invalid Kokoro voice")
    if speed <= 0:
        raise HTTPException(400, "Speed must be positive")
    try:
        wav_bytes = _synthesize(t, voice, lang, speed)
    except RuntimeError as e:
        raise HTTPException(503, f"{e}. Run korvo-server/setup.sh to install/download Kokoro.") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Kokoro synthesis failed: {e}") from e
    return StreamingResponse(io.BytesIO(wav_bytes), media_type="audio/wav")


@router.post("/kokoro/play-local")
def kokoro_play_local(body: KokoroSpeakBody):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Text is required")
    if body.voice not in KOKORO_VOICES:
        raise HTTPException(400, "Invalid Kokoro voice")
    if body.speed <= 0:
        raise HTTPException(400, "Speed must be positive")
    try:
        wav_bytes = _synthesize(text, body.voice, body.lang, body.speed)
    except RuntimeError as e:
        raise HTTPException(503, f"{e}. Run korvo-server/setup.sh to install/download Kokoro.") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Kokoro synthesis failed: {e}") from e

    # OS playback on the server host (macOS afplay).
    if os.name != "posix":
        raise HTTPException(400, "Local playback is supported on macOS/Linux hosts only")
    if not shutil.which("afplay"):
        raise HTTPException(503, "afplay not found on server host")
    try:
        with tempfile.NamedTemporaryFile(prefix="korvo-kokoro-", suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            wav_path = f.name
        subprocess.Popen(["afplay", wav_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Local playback failed: {e}") from e
    return {"success": True, "played_on_server": True}


def _polly_client(access_key_id: str, secret_access_key: str, region: str, session_token: str = ""):
    kwargs = {
        "aws_access_key_id": access_key_id,
        "aws_secret_access_key": secret_access_key,
        "region_name": region,
    }
    if session_token:
        kwargs["aws_session_token"] = session_token
    return boto3.client("polly", **kwargs)


def synthesize_polly(
    text: str,
    language_code: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    session_token: str = "",
) -> bytes:
    voice_by_lang = {
        "en-us": "Joanna",
        "es": "Lucia",
        "fr-fr": "Lea",
        "it": "Bianca",
        "pt-br": "Camila",
        "ja": "Mizuki",
        "hi": "Aditi",
        "zh": "Zhiyu",
    }
    voice = voice_by_lang.get(language_code, "Joanna")
    client = _polly_client(access_key_id, secret_access_key, region, session_token)
    resp = client.synthesize_speech(
        Text=text,
        OutputFormat="pcm",
        VoiceId=voice,
        SampleRate="16000",
    )
    pcm = resp["AudioStream"].read()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm)
    return buf.getvalue()


@router.post("/polly/validate")
def polly_validate(body: PollyValidateBody):
    if not body.aws_access_key_id.strip() or not body.aws_secret_access_key.strip():
        raise HTTPException(400, "AWS credentials are required")
    try:
        client = _polly_client(
            body.aws_access_key_id.strip(),
            body.aws_secret_access_key.strip(),
            (body.aws_region or "eu-west-1").strip(),
            body.aws_session_token.strip(),
        )
        # Quick auth check with minimal payload.
        client.describe_voices(MaxResults=1)
        return {"valid": True, "region": (body.aws_region or "eu-west-1").strip()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"AWS Polly validation failed: {e}") from e
