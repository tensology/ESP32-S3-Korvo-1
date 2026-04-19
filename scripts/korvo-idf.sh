#!/usr/bin/env bash
# Run idf.py from korvo-app after sourcing repo setup.sh (used by korvo-config-server /api/flash).
set -e
REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
SETUP="$REPO/setup.sh"
APP="$REPO/korvo-app"
if [[ ! -f "$SETUP" ]]; then
  echo "korvo-idf: missing setup.sh at $SETUP" >&2
  exit 2
fi
if [[ ! -d "$APP" ]]; then
  echo "korvo-idf: missing korvo-app at $APP" >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$SETUP"
# Node / Cursor often exports IDF_PROJECT_DIR from another tree; idf.py honors it over cwd.
unset IDF_PROJECT_DIR EXTRA_COMPONENT_DIRS IDF_COMPONENT_DIRS 2>/dev/null || true
cd "$APP"
export IDF_PROJECT_DIR="$PWD"
exec idf.py "$@"
