#!/usr/bin/env bash
# Record Korvo mic to korvo-server/recordings/*.wav (VLC-ready). No jq required.
# To save under scripts/stream-captures/ without korvo-server, use: ./scripts/stream_to_wav.sh
#
# Env:
#   KORVO_BOARD_IP     LAN IP of the board (default: 192.168.1.27)
#   KORVO_SERVER       korvo-server base URL (default: http://127.0.0.1:3333)
#   KORVO_RECORD_SEC   seconds (default: 30, max 600)
#   KORVO_PLAY_FFPLAY  if 1, also play while recording (needs ffplay on PATH)
#
# Examples:
#   ./scripts/record.sh
#   KORVO_BOARD_IP=192.168.1.5 KORVO_RECORD_SEC=10 ./scripts/record.sh
set -euo pipefail
export KORVO_BOARD_IP="${KORVO_BOARD_IP:-192.168.1.27}"
export KORVO_RECORD_SEC="${KORVO_RECORD_SEC:-30}"
export KORVO_PLAY_FFPLAY="${KORVO_PLAY_FFPLAY:-0}"
SERVER="${KORVO_SERVER:-http://127.0.0.1:3333}"
SERVER="${SERVER%/}"

BODY="$(python3 <<'PY'
import json, os
ip = os.environ["KORVO_BOARD_IP"]
sec = float(os.environ.get("KORVO_RECORD_SEC", "30"))
ff = os.environ.get("KORVO_PLAY_FFPLAY", "0") == "1"
print(json.dumps({"board_url": f"http://{ip}/api/audio/stream", "duration_sec": sec, "play_ffplay": ff}))
PY
)"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
CODE="$(curl -sS -o "$TMP" -w '%{http_code}' -X POST "${SERVER}/api/audio/record" \
  -H 'Content-Type: application/json' \
  -d "$BODY")"

if [[ "$CODE" != "200" ]]; then
  echo "record failed HTTP $CODE" >&2
  cat "$TMP" >&2
  exit 1
fi

if python3 -m json.tool <"$TMP" 2>/dev/null; then
  :
else
  cat "$TMP"
fi
