from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_suppress_until = 0.0
_suppress_until_by_key: dict[str, float] = {}


def suppress_for(seconds: float, key: str | None = None, max_horizon_sec: float | None = None) -> None:
    dur = max(0.0, float(seconds))
    now = time.monotonic()
    until = now + dur
    if max_horizon_sec is not None:
        cap = max(0.0, float(max_horizon_sec))
        until = min(until, now + cap)
    global _suppress_until
    with _lock:
        if key:
            prev = _suppress_until_by_key.get(key, 0.0)
            if until > prev:
                _suppress_until_by_key[key] = until
            return
        if until > _suppress_until:
            _suppress_until = until


def is_suppressed(key: str | None = None) -> bool:
    with _lock:
        if key:
            return time.monotonic() < _suppress_until_by_key.get(key, 0.0)
        return time.monotonic() < _suppress_until

