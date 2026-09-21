from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from korvo_server.board_auth import set_board_token
from korvo_server.config import API_TOKEN, DB_PATH, HOST, LOGS_DIR, PORT, RECORDINGS_DIR, SERVER_ROOT
from korvo_server.config_gen import generate_config, has_active_wifi
from korvo_server.db import KorvoDB
from korvo_server.flash_env import get_flash_spawn_env
from korvo_server.routers import api_agent, api_arctone, api_audio, api_flash, api_networks, api_pages, api_ports, api_settings, api_transcribe, api_translation, api_tts, api_vendors, api_wifi


@asynccontextmanager
async def lifespan(app: FastAPI):
    kdb = KorvoDB(DB_PATH)
    app.state.kdb = kdb
    try:
        get_flash_spawn_env()
    except Exception as e:
        print(f"[korvo] flash env warmup: {e}")
    with kdb.lock:
        try:
            generate_config(kdb.conn, preserve_wifi_if_missing=not has_active_wifi(kdb.conn))
        except Exception as e:
            print(f"[korvo] initial generate_config: {e}")
    set_board_token(kdb.board_token)
    yield
    kdb.close()


class DashboardTokenMiddleware(BaseHTTPMiddleware):
    """Used only when the server is not bound to loopback."""

    async def dispatch(self, request: Request, call_next):
        got = request.headers.get("x-korvo-token", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        if not API_TOKEN or got != API_TOKEN:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)


app = FastAPI(title="Korvo Server", lifespan=lifespan)
if HOST not in ("127.0.0.1", "localhost", "::1"):
    app.add_middleware(DashboardTokenMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://127.0.0.1:{PORT}",
        f"http://localhost:{PORT}",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Korvo-Token", "Authorization"],
)

static_dir = SERVER_ROOT / "korvo_server" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
(RECORDINGS_DIR / ".gitkeep").touch(exist_ok=True)
app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")
LOGS_DIR.mkdir(parents=True, exist_ok=True)

for r in (
    api_pages.router,
    api_networks.router,
    api_wifi.router,
    api_settings.router,
    api_ports.router,
    api_flash.router,
    api_agent.router,
    api_arctone.router,
    api_vendors.router,
    api_translation.router,
    api_tts.router,
    api_audio.router,
    api_transcribe.router,
):
    app.include_router(r)


def main():
    import uvicorn

    if HOST not in ("127.0.0.1", "localhost", "::1") and not API_TOKEN:
        raise SystemExit("KORVO_API_TOKEN is required when KORVO_HOST is not loopback")
    uvicorn.run("korvo_server.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
