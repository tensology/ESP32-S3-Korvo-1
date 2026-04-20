#!/usr/bin/env python3
"""
40-second Korvo stream capture with on-screen timing so you know when to talk.
Afterwards: compares PCM energy in the SPEAK window vs quiet windows (not VAD — level check).

Uses same URL env as listen.sh / detect_korvo_stream.py.

Timeline (default 40s):
  0–12 s   stay quiet
  12–30 s  speak (script prints when to start)
  30–40 s  quiet again

Exit 0 if stream OK and speak-window peak clearly exceeds both quiet windows.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import threading
import time
import urllib.error
import urllib.request

# Import sibling module (both live in scripts/)
import detect_korvo_stream as dk

PCM_BPS = dk.NOMINAL_PCM_BPS


def _segment_metrics(buf: bytes, pcm_off: int, rel_start: int, rel_end: int) -> dict[str, float | int]:
    rel_start = max(0, rel_start)
    rel_end = max(rel_start, rel_end)
    raw = buf[pcm_off + rel_start : pcm_off + rel_end]
    n = len(raw) // 2
    if n == 0:
        return {"peak": 0, "rms": 0.0, "bytes": 0, "samples": 0}
    peak = 0
    acc = 0.0
    for i in range(n):
        v = struct.unpack_from("<h", raw, i * 2)[0]
        a = abs(v)
        if a > peak:
            peak = a
        acc += float(v) * float(v)
    return {"peak": peak, "rms": (acc / n) ** 0.5, "bytes": len(raw), "samples": n}


def _prompts(stop_evt: threading.Event, total: float, t_quiet_end: float, t_speak_end: float) -> None:
    """Print instructions on a wall-clock schedule until stop_evt."""
    start = time.monotonic()
    printed = set()

    def mark(key: str, msg: str) -> None:
        if key in printed:
            return
        printed.add(key)
        print(msg, file=sys.stderr, flush=True)

    while not stop_evt.is_set():
        now = time.monotonic() - start
        if now >= total:
            break
        if now >= 0 and "intro" not in printed:
            mark(
                "intro",
                "\n=== Korvo stream check (~{0:.0f}s wall clock) ===\n"
                "  {1:.0f}s–{2:.0f}s: stay QUIET (no talking).\n"
                "  {2:.0f}s–{3:.0f}s: when you see SPEAK NOW, talk loudly.\n"
                "  {3:.0f}s–{0:.0f}s: quiet again.\n".format(total, 0.0, t_quiet_end, t_speak_end),
            )
        if now >= t_quiet_end - 2 and "warn_speak" not in printed:
            mark("warn_speak", f"\n>>> In ~2s: start speaking until {t_speak_end:.0f}s on this clock <<<\n")
        if now >= t_quiet_end and "speak" not in printed:
            mark(
                "speak",
                "\n>>> SPEAK NOW (say: ONE TWO THREE — testing the microphone) <<<\n",
            )
        if now >= t_speak_end and "quiet2" not in printed:
            mark("quiet2", "\n>>> STOP. Stay quiet until the end. <<<\n")
        time.sleep(0.15)
    mark("done", "\n=== Capture finished, analysing… ===\n")


def main() -> int:
    p = argparse.ArgumentParser(description="Timed 40s stream + speak-window energy check.")
    p.add_argument("--seconds", type=float, default=40.0, help="Total capture time (default 40)")
    p.add_argument("--quiet1", type=float, default=12.0, help="Seconds of initial silence (default 12)")
    p.add_argument("--speak", type=float, default=18.0, help="Seconds for speak window (default 18)")
    p.add_argument(
        "--min-speak-peak",
        type=int,
        default=400,
        help="Min |int16| peak in speak window to count as plausible voice (default 400)",
    )
    p.add_argument(
        "--ratio",
        type=float,
        default=3.0,
        help="Speak peak must exceed max(quiet peaks) * this ratio (default 3)",
    )
    p.add_argument("--json", action="store_true", help="Print JSON summary on stdout")
    args = p.parse_args()

    total = max(10.0, float(args.seconds))
    t1 = max(2.0, float(args.quiet1))
    t2 = t1 + max(4.0, float(args.speak))
    if t2 >= total - 1.5:
        t2 = total - 2.0
        t1 = max(2.0, min(t1, t2 - 4.0))

    url, via = dk._resolve_stream_url()
    pcm_off, wav_info = 0, {}
    body = bytearray()
    err = None
    http_status = None

    stop_prompts = threading.Event()
    th = threading.Thread(target=_prompts, args=(stop_prompts, total, t1, t2), daemon=True)
    th.start()

    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET", headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=total + 25.0) as resp:
            http_status = getattr(resp, "status", None) or resp.getcode()
            deadline = time.monotonic() + total
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
    finally:
        stop_prompts.set()
    th.join(timeout=2.0)

    elapsed = max(time.monotonic() - t0, 1e-6)
    pcm_off, wav_info = dk._parse_wav_pcm_start(bytes(body))
    if pcm_off == 0 and wav_info.get("ok_wav_container") and len(body) >= 44:
        pcm_off = 44

    b0 = int(t1 * PCM_BPS)
    b1 = int(t2 * PCM_BPS)
    b_end = len(body) - pcm_off
    seg_before = _segment_metrics(bytes(body), pcm_off, 0, min(b0, b_end))
    seg_speak = _segment_metrics(bytes(body), pcm_off, min(b0, b_end), min(b1, b_end))
    seg_after = _segment_metrics(bytes(body), pcm_off, min(b1, b_end), b_end)

    qmax = max(int(seg_before["peak"]), int(seg_after["peak"]), 1)
    speak_peak = int(seg_speak["peak"])
    ratio_ok = speak_peak >= qmax * float(args.ratio)
    level_ok = speak_peak >= int(args.min_speak_peak)
    ok_stream = err is None and (http_status is None or 200 <= http_status < 300) and len(body) > 8000
    ok_speech = ok_stream and ratio_ok and level_ok and int(seg_speak["samples"]) > 1000

    out = {
        "url": url,
        "via": via,
        "seconds_planned": total,
        "elapsed_sec": round(elapsed, 3),
        "bytes_received": len(body),
        "http_status": http_status,
        "error": err,
        "windows_sec": {"quiet_before": [0, t1], "speak": [t1, t2], "quiet_after": [t2, total]},
        "wav": wav_info,
        "pcm_start_offset": pcm_off,
        "pcm_bytes_per_sec": round((len(body) - pcm_off) / elapsed, 1) if pcm_off < len(body) else 0,
        "segment_before": seg_before,
        "segment_speak": seg_speak,
        "segment_after": seg_after,
        "quiet_max_peak": qmax,
        "speak_peak": speak_peak,
        "ratio_speak_to_quiet": round(speak_peak / qmax, 2) if qmax else None,
        "ok_stream": ok_stream,
        "ok_speech_bump": ok_speech,
        "thresholds": {"min_speak_peak": int(args.min_speak_peak), "min_ratio": float(args.ratio)},
    }

    if args.json:
        out["ok"] = ok_speech
        print(json.dumps(out, indent=2))
    else:
        print(f"URL ({via}): {url}", file=sys.stderr)
        if err:
            print(f"Error: {err}", file=sys.stderr)
        print(f"Bytes: {len(body)}  elapsed: {elapsed:.2f}s", file=sys.stderr)
        print(f"Quiet-before peak={seg_before['peak']}  SPEAK peak={seg_speak['peak']}  quiet-after peak={seg_after['peak']}", file=sys.stderr)
        print(f"Need speak_peak>={args.min_speak_peak} and >={args.ratio}x quiet max ({qmax}) -> ratio_ok={ratio_ok} level_ok={level_ok}", file=sys.stderr)
        print("RESULT: " + ("OK — likely heard you in the speak window" if ok_speech else "FAIL — no clear speech bump (or stream broken)"), file=sys.stderr)

    return 0 if ok_speech else 1


if __name__ == "__main__":
    sys.exit(main())
