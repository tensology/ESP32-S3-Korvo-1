"""Mirror Node getFlashSpawnEnv: full PATH after sourcing repo setup.sh for idf.py subprocesses."""

from __future__ import annotations

import os
import subprocess
from typing import Dict

from korvo_server.config import REPO_ROOT, SETUP_SH

_flash_spawn_env_cache: Dict[str, str] | None = None


def _strip_leaky_idf_vars(env: Dict[str, str]) -> None:
    for k in ("IDF_PROJECT_DIR", "EXTRA_COMPONENT_DIRS", "IDF_COMPONENT_DIRS"):
        env.pop(k, None)


def get_flash_spawn_env() -> Dict[str, str]:
    global _flash_spawn_env_cache
    if _flash_spawn_env_cache is not None:
        return _flash_spawn_env_cache
    base = dict(os.environ)
    base.setdefault("HOME", os.environ.get("USERPROFILE", ""))
    if not SETUP_SH.is_file():
        fb = {**base, "PYTHONUNBUFFERED": "1"}
        _strip_leaky_idf_vars(fb)
        _flash_spawn_env_cache = fb
        return _flash_spawn_env_cache
    try:
        proc = subprocess.run(
            ["/bin/bash", "-c", f'set -e && source "{SETUP_SH}" >/dev/null && env'],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=base,
        )
        merged = {**base}
        for line in (proc.stdout or "").splitlines():
            eq = line.find("=")
            if eq <= 0:
                continue
            k, v = line[:eq], line[eq + 1 :]
            if k:
                merged[k] = v
        merged["PYTHONUNBUFFERED"] = "1"
        _strip_leaky_idf_vars(merged)
        _flash_spawn_env_cache = merged
        print(f"[korvo] flash env cached (PATH len={len(merged.get('PATH', ''))})")
        return _flash_spawn_env_cache
    except Exception as e:
        print(f"[korvo] getFlashSpawnEnv failed, using process.env: {e}")
        fb = {**base, "PYTHONUNBUFFERED": "1"}
        _strip_leaky_idf_vars(fb)
        _flash_spawn_env_cache = fb
        return _flash_spawn_env_cache
