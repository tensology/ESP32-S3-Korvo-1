#!/usr/bin/env bash
# Save Korvo HTTP mic stream to scripts/stream-captures/*.wav (VLC-ready RIFF sizes).
#
# Env: same as listen.sh (KORVO_BOARD_IP, KORVO_LISTEN_MODE, KORVO_SERVER, KORVO_STREAM_URL)
#      KORVO_OUT  optional output .wav path
#      KORVO_RECORD_SEC  duration seconds (default 30) — also passed as --seconds
#
# Examples:
#   ./scripts/stream_to_wav.sh
#   KORVO_RECORD_SEC=15 KORVO_LISTEN_MODE=direct KORVO_BOARD_IP=192.168.1.27 ./scripts/stream_to_wav.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export KORVO_RECORD_SEC="${KORVO_RECORD_SEC:-30}"
exec python3 "$ROOT/scripts/stream_to_wav.py" --seconds "$KORVO_RECORD_SEC"
