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

# Clear port immediately before bind (avoids uvicorn ERROR: address already in use).
prepare_port "$PORT"

exec uvicorn korvo_server.main:app --host 0.0.0.0 --port "$PORT" "$@"
