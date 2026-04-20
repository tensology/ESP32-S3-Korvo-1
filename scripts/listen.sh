#!/usr/bin/env bash
# If ./listen.sh says "permission denied", run: chmod +x scripts/listen.sh
# or: bash scripts/listen.sh
#
# Live mic from Korvo: through korvo-server relay (default) or direct to the board.
# Requires: Python 3 + ffplay (see play-korvo-audio.sh).
#
# Env:
#   KORVO_BOARD_HOST   hostname or IP (default: korvo.local)
#   KORVO_STREAM_URL   full board stream URL (overrides host/path default)
#   KORVO_SERVER       korvo-server base URL (default: http://127.0.0.1:3333)
#   KORVO_LISTEN_MODE  relay | direct  (default: relay)
#   KORVO_PLAY_VERBOSE set to 1 for byte-rate + curl/ffplay diagnostics on stderr
#
# Examples:
#   ./scripts/listen.sh
#   KORVO_BOARD_IP=192.168.1.27 ./scripts/listen.sh
#   KORVO_LISTEN_MODE=direct KORVO_BOARD_HOST=192.168.1.27 ./scripts/listen.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${KORVO_BOARD_HOST:-${KORVO_BOARD_IP:-korvo.local}}"
HOST="${HOST#http://}"
HOST="${HOST#https://}"
HOST="${HOST%%/*}"
if [[ -n "${KORVO_STREAM_URL:-}" ]]; then
  STREAM="$KORVO_STREAM_URL"
else
  STREAM="http://${HOST}/api/audio/stream"
fi
SERVER="${KORVO_SERVER:-http://127.0.0.1:3333}"
SERVER="${SERVER%/}"
MODE="${KORVO_LISTEN_MODE:-relay}"

# Avoid "${array[@]}" when empty: with set -u, some bash builds treat that as unbound.
if [[ "$MODE" == "direct" ]]; then
  if [[ "${KORVO_PLAY_VERBOSE:-0}" == "1" ]]; then
    exec "$ROOT/scripts/play-korvo-audio.sh" -v "$STREAM"
  fi
  exec "$ROOT/scripts/play-korvo-audio.sh" "$STREAM"
fi

ENC="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$STREAM")"
RELAY="${SERVER}/api/audio/relay?url=${ENC}"
if [[ "${KORVO_PLAY_VERBOSE:-0}" == "1" ]]; then
  exec "$ROOT/scripts/play-korvo-audio.sh" -v "$RELAY"
fi
exec "$ROOT/scripts/play-korvo-audio.sh" "$RELAY"
