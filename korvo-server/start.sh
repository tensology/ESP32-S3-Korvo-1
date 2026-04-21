#!/usr/bin/env bash
# Start Korvo Server (FastAPI on port 3333).
set -euo pipefail
cd "$(dirname "$0")"

PORT=3333

# PIDs using this port (listeners first; include generic match so nothing is left listening).
_pids_on_port() {
  local port="$1"
  {
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true
    lsof -ti :"$port" 2>/dev/null || true
  } | sort -nu
}

prepare_port() {
  local port="$1"
  if ! command -v lsof >/dev/null 2>&1; then
    echo "Warning: lsof not found; cannot clear port ${port}." >&2
    return 0
  fi
  local pid_list=()
  local p
  while IFS= read -r p; do
    [[ -n "$p" ]] && pid_list+=("$p")
  done < <(_pids_on_port "$port")

  if ((${#pid_list[@]} > 0)); then
    echo "Stopping process(es) on port ${port}: ${pid_list[*]}"
    kill "${pid_list[@]}" 2>/dev/null || true
    sleep 1
    pid_list=()
    while IFS= read -r p; do
      [[ -n "$p" ]] && pid_list+=("$p")
    done < <(_pids_on_port "$port")
    if ((${#pid_list[@]} > 0)); then
      kill -9 "${pid_list[@]}" 2>/dev/null || true
    fi
  fi

  local n=0
  while lsof -nP -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; do
    n=$((n + 1))
    if ((n > 40)); then
      echo "Port ${port} is still in use; try: lsof -nP -iTCP:${port} -sTCP:LISTEN" >&2
      exit 1
    fi
    sleep 0.1
  done
}

bash "$(dirname "$0")/setup.sh"
# shellcheck source=/dev/null
source korvo/bin/activate

# Ensure Kokoro assets are present at startup (not just install time).
KOKORO_DIR="$(dirname "$0")/downloads/kokoro"
KOKORO_MODEL="$KOKORO_DIR/kokoro-v1.0.onnx"
KOKORO_VOICES="$KOKORO_DIR/voices-v1.0.bin"
if [[ ! -s "$KOKORO_MODEL" || ! -s "$KOKORO_VOICES" ]]; then
  echo "[korvo] Kokoro assets missing, fetching before server start..."
  if [[ -x "$(dirname "$0")/bin/download-kokoro.sh" ]]; then
    bash "$(dirname "$0")/bin/download-kokoro.sh"
  fi
fi

if [[ ! -s "$KOKORO_MODEL" || ! -s "$KOKORO_VOICES" ]]; then
  echo "[korvo] Warning: Kokoro assets are still missing; Speak Target may fail." >&2
fi

# Clear port immediately before bind (avoids uvicorn ERROR: address already in use).
prepare_port "$PORT"

exec uvicorn korvo_server.main:app --host 0.0.0.0 --port "$PORT" "$@"
