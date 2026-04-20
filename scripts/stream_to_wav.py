#!/usr/bin/env python3
"""
Pull the Korvo HTTP mic WAV stream into a file under this repo (scripts/stream-captures/).

Patches RIFF/data sizes so VLC / QuickTime / ffplay open the file (firmware uses placeholder sizes).

Env (same as listen.sh):
  KORVO_STREAM_URL, KORVO_BOARD_HOST, KORVO_BOARD_IP, KORVO_SERVER, KORVO_LISTEN_MODE
  KORVO_OUT          optional full path to .wav (default: scripts/stream-captures/korvo_stream_<stamp>.wav)

Examples:
  python3 scripts/stream_to_wav.py --seconds 15
  KORVO_LISTEN_MODE=direct KORVO_BOARD_IP=192.168.1.27 python3 scripts/stream_to_wav.py -s 20
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_scripts = Path(__file__).resolve().parent
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
import detect_korvo_stream as dk


def finalize_wav_header(path: Path) -> None:
    size = path.stat().st_size
    if size < 44:
        return
    with path.open("r+b") as f:
        hdr = bytearray(f.read(44))
        if hdr[0:4] != b"RIFF" or hdr[8:12] != b"WAVE" or hdr[36:40] != b"data":
            return
        struct.pack_into("<I", hdr, 4, size - 8)
        struct.pack_into("<I", hdr, 40, size - 44)
        f.seek(0)
        f.write(hdr)


def main() -> int:
    p = argparse.ArgumentParser(description="Save Korvo HTTP WAV stream to scripts/stream-captures/")
    p.add_argument("-s", "--seconds", type=float, default=30.0, help="Record length (default 30)")
    args = p.parse_args()
    sec = max(0.5, min(float(args.seconds), 600.0))

    url, via = dk._resolve_stream_url()
    out_env = os.environ.get("KORVO_OUT", "").strip()
    if out_env:
        out = Path(out_env).expanduser().resolve()
    else:
        cap = _scripts / "stream-captures"
        cap.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = cap / f"korvo_stream_{stamp}.wav"

    if out.suffix.lower() != ".wav":
        out = out.with_suffix(".wav")
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"URL ({via}): {url}", file=sys.stderr)
    print(f"Writing ~{sec}s to {out}", file=sys.stderr)

    t0 = time.monotonic()
    n = 0
    err = None
    try:
        req = urllib.request.Request(url, method="GET", headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=sec + 30.0) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if code is None or not (200 <= int(code) < 300):
                err = f"HTTP {code}"
            else:
                with out.open("wb") as wf:
                    deadline = time.monotonic() + sec
                    while time.monotonic() < deadline:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        wf.write(chunk)
                        n += len(chunk)
    except urllib.error.HTTPError as e:
        err = f"HTTP {e.code}"
    except urllib.error.URLError as e:
        err = str(e.reason if hasattr(e, "reason") else e)
    except Exception as e:
        err = str(e)

    elapsed = time.monotonic() - t0
    if err:
        print(f"Error: {err}", file=sys.stderr)
        if out.exists() and out.stat().st_size == 0:
            out.unlink(missing_ok=True)
        return 1

    finalize_wav_header(out)
    print(f"Saved {n} bytes in {elapsed:.2f}s -> {out}", file=sys.stderr)
    print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
