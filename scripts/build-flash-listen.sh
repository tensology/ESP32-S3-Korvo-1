#!/usr/bin/env bash
# After build: run FLASH and a LISTEN-prep pipeline IN PARALLEL so you do not sit between steps.
# The listener waits until flash FINISHES, then for the board HTTP stream, then starts listen.sh
# (syncs playback with the firmware that was just pushed).
#
# Env:
#   KORVO_SERIAL_PORT  default /dev/cu.usbserial-11210
#   KORVO_BOARD_IP     board LAN IP for stream probe + listen (default 192.168.1.27)
#   KORVO_SERVER       korvo-server for relay (default http://127.0.0.1:3333)
#   KORVO_PLAY_VERBOSE     set to 1 to pass -v into play_korvo_board_audio.py
#   KORVO_SERIAL_MONITOR   set to 1 to run idf.py monitor in the background once the
#                          stream is up (UART is free after flash; pairs with listen)
#
# Usage:
#   ./scripts/build-flash-listen.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${KORVO_SERIAL_PORT:-/dev/cu.usbserial-11210}"
export KORVO_BOARD_IP="${KORVO_BOARD_IP:-192.168.1.27}"
export KORVO_BOARD_HOST="${KORVO_BOARD_HOST:-$KORVO_BOARD_IP}"
export KORVO_SERVER="${KORVO_SERVER:-http://127.0.0.1:3333}"

FLAG="${TMPDIR:-/tmp}/korvo_flash_done_$$"
rm -f "$FLAG"
trap 'rm -f "$FLAG" 2>/dev/null || true' EXIT

cd "$ROOT/korvo-app"
echo "[korvo] === build ===" >&2
bash "$ROOT/scripts/korvo-idf.sh" build

stream_ok() {
  curl -sf --max-time 2 -o /dev/null "http://${KORVO_BOARD_IP}/api/audio/stream"
}

run_flash() {
  set +e
  bash "$ROOT/scripts/korvo-idf.sh" -p "$PORT" flash
  ec=$?
  set -e
  if [[ "$ec" -eq 0 ]]; then
    touch "$FLAG"
  fi
  exit "$ec"
}

run_listen_after_flash() {
  echo "[korvo] listen-prep: waiting for flash to finish..." >&2
  while [[ ! -f "$FLAG" ]]; do
    sleep 0.25
  done
  echo "[korvo] listen-prep: flash exited; waiting for boot + HTTP stream..." >&2
  sleep 2.5
  local n=0
  while ! stream_ok; do
    sleep 0.4
    n=$((n + 1))
    if (( n % 20 == 0 )); then
      echo "[korvo] still waiting for http://${KORVO_BOARD_IP}/api/audio/stream ..." >&2
    fi
  done
  echo "[korvo] stream is up — starting listen (Ctrl+C stops ffplay)" >&2
  if [[ "${KORVO_SERIAL_MONITOR:-0}" == "1" ]]; then
    (
      cd "$ROOT/korvo-app"
      bash "$ROOT/scripts/korvo-idf.sh" -p "$PORT" monitor
    ) &
    echo "[korvo] idf.py monitor running in background (pid $!); stop with: kill $!" >&2
  fi
  exec bash "$ROOT/scripts/listen.sh"
}

echo "[korvo] === flash (parallel with listen-prep) ===" >&2
run_flash &
FLASH_PID=$!
run_listen_after_flash &
LISTEN_PID=$!

FLASH_EC=0
if ! wait "$FLASH_PID"; then
  FLASH_EC=$?
fi
if [[ "$FLASH_EC" != "0" ]]; then
  echo "[korvo] flash failed (exit $FLASH_EC); stopping listen-prep." >&2
  kill "$LISTEN_PID" 2>/dev/null || true
  wait "$LISTEN_PID" 2>/dev/null || true
  exit "$FLASH_EC"
fi

echo "[korvo] flash OK; waiting on listen (foreground)..." >&2
wait "$LISTEN_PID"
