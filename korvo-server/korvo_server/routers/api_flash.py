import asyncio
import json
import os
import re
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from korvo_server.config import KORVO_APP, KORVO_IDF_SH, REPO_ROOT, SETUP_SH
from korvo_server.config_gen import generate_config, has_active_wifi
from korvo_server.deps import KorvoDep
from korvo_server.flash_env import get_flash_spawn_env

router = APIRouter(prefix="/api", tags=["flash"])

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07")


class FlashBody(BaseModel):
    port: str
    clean_before_build: bool = False


class ResetBody(BaseModel):
    port: str = ""


def _normalize_serial_port(raw: str) -> str:
    candidate = (raw or "").strip()
    if not candidate:
        return ""
    port = candidate if candidate.startswith("/dev/") else f"/dev/{candidate}"
    port_ok = bool(re.match(r"^/dev/cu\.[^/]+$", port) or re.match(r"^/dev/tty(USB|ACM)[0-9]+$", port))
    return port if port_ok else ""


def _rank_port_name(name: str) -> int:
    s = name.lower()
    if "usbserial" in s:
        return 0
    if "wchusb" in s or "usbmodem" in s:
        return 1
    if "slab" in s:
        return 2
    return 5


def _auto_detect_serial_port() -> str:
    try:
        dev = Path("/dev")
        names = sorted(
            [p.name for p in dev.iterdir() if p.name.startswith("cu.")],
            key=lambda n: (_rank_port_name(n), n),
        )
    except OSError:
        return ""
    for f in names:
        rest = f[3:]
        if re.match(r"^(Bluetooth|debug-console|JBL|GroundControl|AVT)", rest, re.I):
            continue
        if re.search(r"usb|serial|SLAB|wch|modem|acm", rest, re.I):
            p = f"/dev/{f}"
            if _normalize_serial_port(p):
                return p
    return ""


def _force_reset_with_serial_pulse(port: str, spawn_env: dict[str, str]) -> tuple[bool, str]:
    pulse_code = (
        "import sys,time\n"
        "import serial\n"
        "p=sys.argv[1]\n"
        "s=serial.Serial(p,115200,timeout=0.2)\n"
        "s.dtr=False\n"
        "s.rts=True\n"
        "time.sleep(0.12)\n"
        "s.rts=False\n"
        "time.sleep(0.12)\n"
        "s.close()\n"
        "print('reset_pulse_sent')\n"
    )
    try:
        proc = subprocess.run(
            ["python3", "-c", pulse_code, port],
            cwd=str(REPO_ROOT),
            env=spawn_env,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as e:
        return False, f"serial_pulse_exception: {e}"
    if proc.returncode == 0:
        return True, (proc.stdout or "reset_pulse_sent").strip()
    err = (proc.stderr or proc.stdout or "").strip()
    return False, f"serial_pulse_failed: {err}"


def _force_reset_with_esptool(port: str, spawn_env: dict[str, str]) -> tuple[bool, str]:
    idf_home = spawn_env.get("IDF_PATH") or str(Path(spawn_env.get("HOME", os.environ.get("HOME", ""))) / "esp" / "esp-idf")
    esp_tool = Path(idf_home) / "components" / "esptool_py" / "esptool" / "esptool.py"
    if not esp_tool.is_file():
        return False, f"esptool_not_found: {esp_tool}"
    try:
        proc = subprocess.run(
            [
                "python3",
                str(esp_tool),
                "--chip",
                "auto",
                "-p",
                port,
                "--before",
                "default_reset",
                "--after",
                "hard_reset",
                "chip_id",
            ],
            cwd=str(REPO_ROOT),
            env=spawn_env,
            capture_output=True,
            text=True,
            timeout=16,
        )
    except Exception as e:
        return False, f"esptool_exception: {e}"
    if proc.returncode == 0:
        return True, "esptool_reset_sent"
    err = (proc.stderr or proc.stdout or "").strip()
    return False, f"esptool_failed: {err}"


def _sse(event: str, data: object) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def _iter_bash_output(args: list[str], cwd: str, env: dict[str, str]):
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash",
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if not proc.stdout:
        code = await proc.wait()
        if code != 0:
            raise RuntimeError(f"Command failed (exit {code})")
        return
    buf = b""
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = ANSI_RE.sub("", line.decode(errors="replace")).strip()
            if text:
                yield text
    if buf.strip():
        text = ANSI_RE.sub("", buf.decode(errors="replace")).strip()
        if text:
            yield text
    code = await proc.wait()
    if code != 0:
        raise RuntimeError(f"Command failed (exit {code})")


@router.get("/flash/diag")
def flash_diag():
    fe = get_flash_spawn_env()
    setup_sh = str(SETUP_SH)
    out: dict = {
        "repo_root": str(REPO_ROOT),
        "wrapper": str(KORVO_IDF_SH),
        "wrapper_exists": KORVO_IDF_SH.is_file(),
        "setup_sh": setup_sh,
        "setup_exists": Path(setup_sh).is_file(),
        "idf_path": fe.get("IDF_PATH") or os.environ.get("IDF_PATH"),
        "path_len_node": len(os.environ.get("PATH", "")),
        "path_len_after_setup": len(fe.get("PATH", "")),
        "idf_py": None,
    }
    try:
        r = subprocess.run(
            ["/bin/bash", "-c", f'source "{setup_sh}" >/dev/null && command -v idf.py'],
            capture_output=True,
            text=True,
            timeout=20,
            env=fe,
            cwd=str(REPO_ROOT),
        )
        out["idf_py"] = (r.stdout or "").strip()
        return out
    except Exception as e:
        out["error"] = str(e)
        return JSONResponse(out, status_code=500)


@router.post("/flash")
async def flash_stream(body: FlashBody, kdb: KorvoDep):
    raw = (body.port or "").strip()
    port = raw if raw.startswith("/dev/") else f"/dev/{raw}"
    port_ok = bool(re.match(r"^/dev/cu\.[^/]+$", port) or re.match(r"^/dev/tty(USB|ACM)[0-9]+$", port))
    if not port_ok:
        raise HTTPException(
            400,
            "Select a serial port such as /dev/cu.usbserial-* (macOS) or /dev/ttyUSB0 (Linux). "
            "Avoid /dev/tty.* on macOS with esptool.",
        )
    if not KORVO_IDF_SH.is_file():
        raise HTTPException(500, f"Missing {KORVO_IDF_SH}. Clone the full Korvo repo (scripts/korvo-idf.sh).")

    project_dir = str(KORVO_APP)
    spawn_env = get_flash_spawn_env()

    async def events() -> AsyncIterator[bytes]:
        yield b": stream-open\n\n"

        def send(event: str, data: object) -> bytes:
            return _sse(event, data)

        try:
            yield send(
                "output",
                {
                    "text": f"[flash] port={port} clean_before_build={body.clean_before_build}",
                    "source": "system",
                },
            )
            with kdb.lock:
                try:
                    generate_config(
                        kdb.conn,
                        preserve_wifi_if_missing=not has_active_wifi(kdb.conn),
                    )
                    yield send("output", {"text": "Config generated", "source": "system"})
                except Exception as e:
                    yield send("output", {"text": f"❌ generateConfig: {e}", "source": "error"})
                    raise

            if body.clean_before_build:
                ninja = Path(project_dir) / "build" / "build.ninja"
                if ninja.is_file():
                    yield send("phase", "Ninja clean (full recompile)…")
                    async for line in _iter_bash_output(["-c", "ninja -C build clean"], project_dir, spawn_env):
                        yield send("output", {"text": line, "source": "stdout"})
                    yield send("output", {"text": "Clean finished — compiling from scratch", "source": "system"})
                else:
                    yield send(
                        "output",
                        {"text": "Skipping ninja clean (no build/ yet)", "source": "system"},
                    )

            yield send("phase", "Building firmware…")
            async for line in _iter_bash_output([str(KORVO_IDF_SH), "build"], project_dir, spawn_env):
                yield send("output", {"text": line, "source": "stdout"})
            yield send("output", {"text": "Build complete", "source": "system"})

            yield send("flash_start", {"port": port})
            yield send("output", {"text": f"→ Esptool will use ONLY this port: {port}", "source": "system"})
            yield send("phase", "Flashing firmware…")

            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                str(KORVO_IDF_SH),
                "-p",
                port,
                "flash",
                cwd=project_dir,
                env=spawn_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout
            buf = b""
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = ANSI_RE.sub("", line.decode(errors="replace")).strip()
                    if not text:
                        continue
                    yield send("output", {"text": text, "source": "stdout"})
                    if "Connecting" in text:
                        yield send("boot_mode", {"message": "Hold BOOT, press RESET, release BOOT", "countdown": 8})
                    if text.startswith("Chip is "):
                        yield send("boot_success", {"message": "Board connected — flashing..."})
                    if "Writing at" in text:
                        yield send("flash_progress", {"text": text})
                    if "Hard resetting" in text:
                        yield send("flash_done", {"message": "Firmware flashed — board resetting"})
            if buf.strip():
                text = ANSI_RE.sub("", buf.decode(errors="replace")).strip()
                if text:
                    yield send("output", {"text": text, "source": "stdout"})
            code = await proc.wait()
            if code != 0:
                raise RuntimeError(f"Flash failed (exit {code})")

            await asyncio.sleep(0.9)
            idf_home = spawn_env.get("IDF_PATH") or str(Path(spawn_env.get("HOME", os.environ.get("HOME", ""))) / "esp" / "esp-idf")
            esp_tool = Path(idf_home) / "components" / "esptool_py" / "esptool" / "esptool.py"
            if esp_tool.is_file():
                try:
                    yield send("phase", "Post-flash: probe chip on selected port…")
                    async for line in _iter_bash_output(
                        ["-c", f'python3 "{esp_tool}" --chip auto -p "{port}" read_mac 2>&1'],
                        project_dir,
                        spawn_env,
                    ):
                        yield send("output", {"text": line, "source": "stdout"})
                except Exception as e:
                    yield send(
                        "output",
                        {"text": f"Post-flash probe failed: {e} — wrong /dev/cu.* or port busy?", "source": "error"},
                    )

            yield send("output", {"text": "Flash complete", "source": "system"})
            yield send("complete", {"success": True, "message": "Firmware built & flashed successfully!"})
        except Exception as err:
            yield send("output", {"text": f"Error: {err}", "source": "error"})
            yield send("complete", {"success": False, "message": str(err)})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/flash/reset")
def flash_force_reset(body: ResetBody):
    port = _normalize_serial_port(body.port) or _auto_detect_serial_port()
    if not port:
        raise HTTPException(400, "No valid serial port found. Connect board and try again.")
    spawn_env = get_flash_spawn_env()
    ok, detail = _force_reset_with_serial_pulse(port, spawn_env)
    if ok:
        return {"success": True, "port": port, "method": "serial_pulse", "detail": detail}
    ok2, detail2 = _force_reset_with_esptool(port, spawn_env)
    if ok2:
        return {"success": True, "port": port, "method": "esptool", "detail": detail2, "fallback_from": detail}
    raise HTTPException(
        500,
        {
            "message": "Force reset failed.",
            "port": port,
            "serial_pulse_error": detail,
            "esptool_error": detail2,
        },
    )
