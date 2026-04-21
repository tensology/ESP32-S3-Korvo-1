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
