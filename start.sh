#!/usr/bin/env bash
# Start Korvo Python server (same as korvo-server/start.sh).
# Repo root also has setup.sh for ESP-IDF — that is unrelated; server bootstrap is korvo-server/setup.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec bash "$ROOT/korvo-server/start.sh" "$@"
