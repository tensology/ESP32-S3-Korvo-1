#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/downloads/kokoro"
mkdir -p "$OUT_DIR"

ONNX_URLS=(
  "https://github.com/thewh1teagle/kokoro-onnx/releases/latest/download/kokoro-v1.0.onnx"
  "https://sourceforge.net/projects/kokoro-onnx.mirror/files/model-files-v1.0/kokoro-v1.0.onnx/download"
)
VOICE_URLS=(
  "https://github.com/thewh1teagle/kokoro-onnx/releases/latest/download/voices-v1.0.bin"
  "https://sourceforge.net/projects/kokoro-onnx.mirror/files/model-files-v1.0/voices-v1.0.bin/download"
)

download_if_missing() {
  local kind="$1"
  local out="$2"
  if [[ -s "$out" ]]; then
    echo "[kokoro] exists: $out"
    return
  fi
  echo "[kokoro] downloading: $out"
  local ok=0
  shift 2
  for url in "$@"; do
    if curl -L --fail "$url" -o "$out"; then
      ok=1
      break
    fi
  done
  if [[ "$ok" -ne 1 ]]; then
    echo "[kokoro] failed to download ${kind}" >&2
    return 1
  fi
}

download_if_missing "model" "$OUT_DIR/kokoro-v1.0.onnx" "${ONNX_URLS[@]}"
download_if_missing "voices" "$OUT_DIR/voices-v1.0.bin" "${VOICE_URLS[@]}"

echo "[kokoro] assets ready in $OUT_DIR"
