#!/usr/bin/env bash
# Play Korvo mic stream with minimal buffering (bypasses heavy browser WAV buffering).
# Uses Python + curl | ffplay (WAV demuxer). Falls back to direct ffplay on URL if no Python.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$ROOT/scripts/play_korvo_board_audio.py" "$@"
fi
URL="${1:-${KORVO_STREAM_URL:-http://korvo.local/api/audio/stream}}"
if ! command -v ffplay >/dev/null 2>&1; then
  echo "Install ffmpeg for ffplay (brew install ffmpeg) and optionally Python 3 for lower latency." >&2
  exit 1
fi
# Direct URL fallback (no Python): do not use tiny probesize — WAV header is 44 bytes.
exec ffplay -nodisp -loglevel warning -fflags nobuffer -flags low_delay -analyzeduration 0 -i "$URL"
