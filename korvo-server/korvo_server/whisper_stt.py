"""
Local speech-to-text via whisper.cpp bindings (pywhispercpp).

Models: short names like ``base.en`` — pywhispercpp downloads GGML weights on first use
(typically under ``~/Library/Application Support/pywhispercpp/models/`` on macOS).

Install (optional): ``pip install 'pywhispercpp @ git+https://github.com/absadiki/pywhispercpp'`` plus numpy.
"""

from __future__ import annotations

import os
import threading
from typing import Any

try:
    import pywhispercpp.model as _pwc

    WHISPER_AVAILABLE = True
except ImportError:
    _pwc = None
    WHISPER_AVAILABLE = False

_model_lock = threading.Lock()
_models: dict[str, Any] = {}


def whisper_ready() -> bool:
    return bool(WHISPER_AVAILABLE)


def get_whisper_model(model_id: str = "base.en") -> Any:
    """Load (or return cached) whisper.cpp model by id (e.g. base.en, small.en)."""
    if not WHISPER_AVAILABLE or _pwc is None:
        raise RuntimeError("pywhispercpp is not installed — see korvo-server/README.md")
    with _model_lock:
        if model_id not in _models:
            try:
                inst = _pwc.Model(model_id, print_progress=False, redirect_whispercpp_logs_to=os.devnull)  # type: ignore[misc]
            except TypeError:
                inst = _pwc.Model(model_id, print_progress=False)  # type: ignore[misc]
            _models[model_id] = inst
        return _models[model_id]


def transcribe_pcm16_mono_s16le(pcm_s16le: bytes, model_id: str = "base.en") -> str:
    """
    Run whisper on mono 16-bit little-endian PCM at 16 kHz (Korvo stream body after WAV header).
    Pads with silence if shorter than ~1s (whisper behaves better with >=1s).
    """
    if not pcm_s16le:
        return ""
    try:
        import numpy as np
    except ImportError as e:
        raise RuntimeError("numpy is required for Whisper transcription") from e
    sample_rate = 16000
    bps = sample_rate * 2
    min_ms = 1000
    cur_ms = (len(pcm_s16le) / bps) * 1000.0
    buf = bytearray(pcm_s16le)
    if cur_ms < min_ms:
        need = int(((min_ms - cur_ms) / 1000.0) * bps)
        buf.extend(b"\x00" * max(0, need))
    audio = np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) * (1.0 / 32768.0)
    model = get_whisper_model(model_id)
    segments = model.transcribe(audio)
    if isinstance(segments, list):
        return " ".join(getattr(s, "text", str(s)) for s in segments).strip()
    if isinstance(segments, dict):
        return str(segments.get("text", "")).strip()
    return str(segments).strip()
