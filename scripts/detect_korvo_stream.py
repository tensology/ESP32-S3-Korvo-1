#!/usr/bin/env python3
"""
Detect Korvo HTTP mic stream: bytes reaching this machine + whether it looks like
correct 16 kHz mono s16le WAV (throughput, header, flatline).

Env (same as scripts/listen.sh):
  KORVO_STREAM_URL, KORVO_BOARD_HOST, KORVO_BOARD_IP, KORVO_SERVER, KORVO_LISTEN_MODE

Exit 0: stream present AND format/throughput checks pass (see --json ok_looks_correct).
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Korvo firmware main.c: mono 16 kHz s16le, ~32000 PCM bytes/s after 44-byte WAV header
NOMINAL_PCM_BPS = 32000
PCM_BPS_MIN = 22000
PCM_BPS_MAX = 42000


def _resolve_stream_url() -> tuple[str, str]:
    explicit = os.environ.get("KORVO_STREAM_URL", "").strip()
    if explicit:
        return explicit, "KORVO_STREAM_URL"
    host = os.environ.get("KORVO_BOARD_HOST", os.environ.get("KORVO_BOARD_IP", "korvo.local")).strip()
    host = host.removeprefix("http://").removeprefix("https://").split("/")[0]
    board = f"http://{host}/api/audio/stream"
    mode = os.environ.get("KORVO_LISTEN_MODE", "relay").strip().lower()
    if mode == "direct":
        return board, "direct"
    server = os.environ.get("KORVO_SERVER", "http://127.0.0.1:3333").rstrip("/")
    q = urllib.parse.quote(board, safe="")
    return f"{server}/api/audio/relay?url={q}", "relay"


def _parse_wav_pcm_start(buf: bytes) -> tuple[int, dict[str, object]]:
    """
    Return (bytes_to_skip_before_pcm, info dict).
    If not a parseable Korvo-style WAV, skip 0 and set ok_wav_fmt false.
    """
    info: dict[str, object] = {
        "ok_wav_container": False,
        "ok_wav_pcm_fmt": False,
        "audio_format": None,
        "channels": None,
        "sample_rate_hz": None,
        "byte_rate": None,
        "bits_per_sample": None,
        "pcm_start_offset": 0,
    }
    if len(buf) < 44 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        return 0, info
    info["ok_wav_container"] = True
    if buf[12:16] != b"fmt ":
        return 0, info
    fmt_size = struct.unpack_from("<I", buf, 16)[0]
    if 20 + fmt_size > len(buf) or fmt_size < 16:
        return 0, info
    audio_format, ch, sr, br, _, bps = struct.unpack_from("<HHIIHH", buf, 20)
    info["audio_format"] = audio_format
    info["channels"] = ch
    info["sample_rate_hz"] = sr
    info["byte_rate"] = br
    info["bits_per_sample"] = bps
    # Expect PCM mono 16 kHz s16le (firmware header)
    ok_fmt = audio_format == 1 and ch == 1 and sr == 16000 and bps == 16 and br == 32000
    info["ok_wav_pcm_fmt"] = ok_fmt
    off = 20 + fmt_size
    if off + 8 > len(buf) or buf[off : off + 4] != b"data":
        # padded fmt or odd layout — still try classic 44-byte layout
        if len(buf) >= 44 and buf[36:40] == b"data":
            info["pcm_start_offset"] = 44
            return 44, info
        return 0, info
    data_size = struct.unpack_from("<I", buf, off + 4)[0]
    info["data_chunk_size"] = data_size
    pcm_off = off + 8
    info["pcm_start_offset"] = pcm_off
    return pcm_off, info


def _pcm_metrics(buf: bytes, pcm_off: int) -> tuple[int, float, int]:
    """peak, rms, sample_count"""
    raw = buf[pcm_off:]
    n = len(raw) // 2
    if n == 0:
        return 0, 0.0, 0
    peak = 0
    acc = 0
    for i in range(n):
        v = struct.unpack_from("<h", raw, i * 2)[0]
        a = abs(v)
        if a > peak:
            peak = a
        acc += v * v
    return peak, (acc / n) ** 0.5, n


def main() -> int:
    p = argparse.ArgumentParser(description="Detect Korvo stream + validate it looks correct.")
    p.add_argument("--seconds", type=float, default=2.5, help="How long to read (default 2.5)")
    p.add_argument("--min-bytes", type=int, default=0, help="Min raw bytes (0 = auto ~40%% nominal)")
    p.add_argument(
        "--allow-flatline",
        action="store_true",
        help="Do not fail if many PCM samples are all zero (quiet room / gain)",
    )
    p.add_argument("--min-peak", type=int, default=48, help="With --check-signal: min |int16| peak")
    p.add_argument("--check-signal", action="store_true", help="Require peak >= --min-peak for exit 0")
    p.add_argument("--json", action="store_true", help="Print JSON on stdout")
    args = p.parse_args()

    url, via = _resolve_stream_url()
    sec = max(0.5, float(args.seconds))
    min_bytes = args.min_bytes if args.min_bytes > 0 else int(NOMINAL_PCM_BPS * sec * 0.35)

    t0 = time.monotonic()
    body = bytearray()
    http_status = None
    err = None
    try:
        req = urllib.request.Request(url, method="GET", headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=min(45.0, sec + 15.0)) as resp:
            http_status = getattr(resp, "status", None) or resp.getcode()
            deadline = time.monotonic() + sec
            while time.monotonic() < deadline:
                chunk = resp.read(65536)
                if not chunk:
                    break
                body.extend(chunk)
    except urllib.error.HTTPError as e:
        http_status = e.code
        err = str(e)
    except urllib.error.URLError as e:
        err = str(e.reason if hasattr(e, "reason") else e)
    except Exception as e:
        err = str(e)

    elapsed = max(time.monotonic() - t0, 1e-6)
    n = len(body)
    ok_http = err is None and (http_status is None or (200 <= http_status < 300))
    ok_bytes = n >= min_bytes and ok_http

    pcm_off, wav_info = _parse_wav_pcm_start(bytes(body))
    if pcm_off == 0 and wav_info.get("ok_wav_container") and len(body) >= 44:
        pcm_off = 44
        wav_info["pcm_start_offset"] = 44
    peak, rms, nsamp = _pcm_metrics(bytes(body), pcm_off)
    pcm_bytes = max(0, n - pcm_off)
    pcm_bps = pcm_bytes / elapsed

    ok_wav_container = bool(wav_info.get("ok_wav_container"))
    ok_wav_pcm_fmt = bool(wav_info.get("ok_wav_pcm_fmt"))
    ok_align = pcm_bytes % 2 == 0
    ok_throughput = pcm_bps >= PCM_BPS_MIN and pcm_bps <= PCM_BPS_MAX
    # Half second of nominal PCM ≈ 16000 samples; if all zero, demux or mic path is suspect
    flatline_threshold_samples = 8000
    ok_not_flatline = not (nsamp >= flatline_threshold_samples and peak == 0 and rms == 0.0)
    if args.allow_flatline:
        ok_not_flatline = True

    warnings: list[str] = []
    if ok_bytes and pcm_off == 0 and n > 100:
        warnings.append("no_riff_wave_prefix_treat_as_raw_pcm_metrics_may_be_off")
    if ok_bytes and ok_wav_container and not ok_wav_pcm_fmt:
        warnings.append("wav_container_ok_but_fmt_not_16k_mono_s16le")
    if ok_bytes and nsamp >= flatline_threshold_samples and peak == 0:
        warnings.append("pcm_flatline_suspect_demux_or_mute")

    ok_signal = peak >= int(args.min_peak)
    ok_looks_correct = (
        ok_bytes
        and ok_wav_container
        and ok_wav_pcm_fmt
        and ok_align
        and ok_throughput
        and ok_not_flatline
    )
    if args.check_signal:
        ok_looks_correct = ok_looks_correct and ok_signal

    out: dict[str, object] = {
        "url": url,
        "via": via,
        "http_status": http_status,
        "error": err,
        "seconds_requested": sec,
        "elapsed_sec": round(elapsed, 3),
        "bytes_received": n,
        "min_bytes_required": min_bytes,
        "ok_stream_to_machine": ok_bytes,
        "wav": wav_info,
        "pcm_start_offset": pcm_off,
        "pcm_bytes": pcm_bytes,
        "pcm_bytes_per_sec": round(pcm_bps, 1),
        "pcm_bps_expected": NOMINAL_PCM_BPS,
        "pcm_bps_band": [PCM_BPS_MIN, PCM_BPS_MAX],
        "ok_wav_container": ok_wav_container,
        "ok_wav_pcm_fmt": ok_wav_pcm_fmt,
        "ok_pcm_alignment": ok_align,
        "ok_throughput_like_16k_mono": ok_throughput,
        "pcm_sample_count": nsamp,
        "pcm_peak_abs": peak,
        "pcm_rms": round(rms, 2),
        "ok_not_flatline_pcm": ok_not_flatline,
        "warnings": warnings,
        "check_signal": bool(args.check_signal),
        "ok_signal_peak": ok_signal if args.check_signal else None,
        "ok_looks_correct": ok_looks_correct,
    }

    if args.json:
        out["ok"] = ok_looks_correct
        print(json.dumps(out, indent=2))
    else:
        print(f"URL ({via}): {url}", file=sys.stderr)
        if err:
            print(f"Error: {err}", file=sys.stderr)
        if http_status is not None:
            print(f"HTTP: {http_status}", file=sys.stderr)
        print(f"Bytes: {n} in {elapsed:.2f}s (min {min_bytes})", file=sys.stderr)
        print(f"WAV: container={ok_wav_container} fmt_16k_mono_s16={ok_wav_pcm_fmt} pcm_off={pcm_off}", file=sys.stderr)
        print(f"PCM: {pcm_bytes} B @ {pcm_bps:.0f} B/s (want ~{NOMINAL_PCM_BPS}, band {PCM_BPS_MIN}-{PCM_BPS_MAX})", file=sys.stderr)
        print(f"PCM: samples={nsamp} peak={peak} rms={rms:.2f} flatline_ok={ok_not_flatline}", file=sys.stderr)
        if warnings:
            for w in warnings:
                print(f"Warning: {w}", file=sys.stderr)
        if args.check_signal:
            print(f"Signal: peak>={args.min_peak} -> {'PASS' if ok_signal else 'FAIL'}", file=sys.stderr)
        print("RESULT: " + ("OK — streaming in and looks correct" if ok_looks_correct else "FAIL"), file=sys.stderr)

    return 0 if ok_looks_correct else 1


if __name__ == "__main__":
    sys.exit(main())
