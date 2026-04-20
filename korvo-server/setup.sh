#!/usr/bin/env bash
# Idempotent: Python venv + pip deps for Korvo server (does not start uvicorn).
# Uses korvo/bin/* explicitly so this can run as bash setup.sh (no shell activation needed).
set -euo pipefail
cd "$(dirname "$0")"

PY="korvo/bin/python"
PIP="korvo/bin/pip"

need_core=0
if [[ ! -x "$PY" ]]; then
  need_core=1
elif ! "$PY" -c "import fastapi, numpy" 2>/dev/null; then
  need_core=1
fi

if [[ "$need_core" == 1 ]]; then
  if [[ ! -d korvo ]]; then
    echo "Creating venv ./korvo ..."
    python3 -m venv korvo
  fi
  echo "Installing core dependencies from requirements.txt ..."
  "$PIP" install -r requirements.txt
fi

if ! "$PY" -c "import pywhispercpp" 2>/dev/null; then
  echo "Installing local Whisper (pywhispercpp from requirements-whisper.txt) ..."
  if ! "$PIP" install -r requirements-whisper.txt; then
    echo "[korvo] Warning: pywhispercpp install failed (needs CMake + C++ toolchain on some systems)." >&2
    echo "[korvo] Server can still start; fix with: korvo/bin/pip install -r requirements-whisper.txt" >&2
  fi
fi
