#!/usr/bin/env python3
"""
Play Korvo firmware HTTP mic stream with low latency.

The board (or korvo-server relay) sends WAV: 44-byte header then s16le mono @ 16 kHz.
We stream bytes into ffplay's WAV demuxer (avoids raw s16le options that break on newer ffmpeg).

Requires: ffplay (ffmpeg). Uses curl when available for robust chunked HTTP; else urllib.

Example:
  ./scripts/play_korvo_board_audio.py http://192.168.1.50/api/audio/stream
  KORVO_STREAM_URL=http://127.0.0.1:3333/api/audio/relay?url=... ./scripts/play_korvo_board_audio.py
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request


def _ffplay_wav_pipe(verbose: bool) -> list[str]:
    ffplay = shutil.which("ffplay")
    if not ffplay:
        return []
    # probesize must exceed 44-byte WAV header; 32 breaks demux → silence on some ffmpeg builds.
    log = "info" if verbose else "warning"
    return [
        ffplay,
        "-nodisp",
        "-loglevel",
        log,
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-analyzeduration",
        "0",
        "-f",
        "wav",
        "-i",
        "pipe:0",
    ]


def _play_via_curl(url: str, ff_args: list[str], verbose: bool) -> int:
    import time

    curl = shutil.which("curl")
    assert curl
    curl_cmd = [curl, "-sS", "-N", "--http1.1", "-H", "Connection: close"]
    if verbose:
        curl_cmd.append("-v")
    curl_cmd.append(url)

    if verbose:
        print(f"[korvo-audio] stream URL: {url}", file=sys.stderr)
        curl_p = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert curl_p.stdout is not None
        ff = subprocess.Popen(ff_args, stdin=subprocess.PIPE, stderr=None)
        assert ff.stdin is not None
        total = 0
        t0 = time.monotonic()
        last = t0
        try:
            while True:
                chunk = curl_p.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                ff.stdin.write(chunk)
                now = time.monotonic()
                if now - last >= 2.0:
                    kb_s = total / max(now - t0, 0.001) / 1024.0
                    print(
                        f"[korvo-audio] received {total} bytes ({kb_s:.1f} KiB/s) — ~31 KiB/s expected for 16 kHz mono s16le",
                        file=sys.stderr,
                    )
                    last = now
        except BrokenPipeError:
            pass
        finally:
            try:
                ff.stdin.close()
            except Exception:
                pass
            if curl_p.stderr:
                err = curl_p.stderr.read()
                if err:
                    sys.stderr.write(err.decode(errors="replace"))
            rc_curl = curl_p.wait()
            rc_ff = ff.wait()
            print(f"[korvo-audio] curl exit={rc_curl} ffplay exit={rc_ff}", file=sys.stderr)
        if rc_curl != 0:
            return int(rc_curl)
        if rc_ff not in (0, None):
            return int(rc_ff)
        return 0

    curl_p = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert curl_p.stdout is not None
    ff = subprocess.Popen(ff_args, stdin=curl_p.stdout, stderr=subprocess.DEVNULL)
    curl_p.stdout.close()
    cerr = curl_p.stderr.read() if curl_p.stderr else b""
    rc_curl = curl_p.wait()
    rc_ff = ff.wait()
    if rc_curl != 0 and cerr:
        print(cerr.decode(errors="replace").strip(), file=sys.stderr)
    if rc_ff not in (0, None):
        return int(rc_ff)
    if rc_curl not in (0, None):
        return int(rc_curl)
    return 0


def _play_via_urllib(url: str, ff_args: list[str], verbose: bool) -> int:
    req = urllib.request.Request(url, method="GET", headers={"Connection": "close"})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
    except urllib.error.URLError as e:
        print(f"Open stream failed: {e}", file=sys.stderr)
        return 1
    ff_err = None if verbose else subprocess.DEVNULL
    ff = subprocess.Popen(ff_args, stdin=subprocess.PIPE, stderr=ff_err)
    assert ff.stdin is not None
    try:
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            ff.stdin.write(chunk)
    except BrokenPipeError:
        pass
    finally:
        try:
            ff.stdin.close()
        except Exception:
            pass
        resp.close()
    ff.wait()
    return ff.returncode if ff.returncode is not None else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Low-latency playback of Korvo /api/audio/stream (or relay URL)")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log byte rate to stderr, curl -v, and ffplay messages (diagnose silence).",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=os.environ.get("KORVO_STREAM_URL", "http://korvo.local/api/audio/stream"),
        help="Full URL to GET stream (default: KORVO_STREAM_URL or korvo.local)",
    )
    args = parser.parse_args()
    verbose = args.verbose or os.environ.get("KORVO_PLAY_VERBOSE", "").strip() in ("1", "true", "yes")

    ff_args = _ffplay_wav_pipe(verbose)
    if not ff_args:
        print("ffplay not found. Install ffmpeg (brew install ffmpeg).", file=sys.stderr)
        return 1

    if shutil.which("curl"):
        return _play_via_curl(args.url, ff_args, verbose)

    print("curl not found; falling back to urllib (chunked streams may fail on some Python versions).", file=sys.stderr)
    return _play_via_urllib(args.url, ff_args, verbose)


if __name__ == "__main__":
    raise SystemExit(main())
