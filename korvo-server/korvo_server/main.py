from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from korvo_server.config import DB_PATH, PORT, RECORDINGS_DIR, SERVER_ROOT
from korvo_server.config_gen import generate_config, has_active_wifi
from korvo_server.db import KorvoDB
from korvo_server.flash_env import get_flash_spawn_env
from korvo_server.routers import api_agent, api_audio, api_flash, api_networks, api_pages, api_ports, api_settings, api_transcribe, api_translation, api_wifi


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
            if has_active_wifi(kdb.conn):
                generate_config(kdb.conn)
            else:
                print("[korvo] No active WiFi in DB — not overwriting korvo_config.* (save WiFi in the UI to sync).")
        except Exception as e:
            print(f"[korvo] initial generate_config: {e}")
    yield
    kdb.close()


app = FastAPI(title="Korvo Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = SERVER_ROOT / "korvo_server" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
(RECORDINGS_DIR / ".gitkeep").touch(exist_ok=True)
app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")

for r in (
    api_pages.router,
    api_networks.router,
    api_wifi.router,
    api_settings.router,
    api_ports.router,
    api_flash.router,
    api_agent.router,
    api_translation.router,
    api_audio.router,
    api_transcribe.router,
):
    app.include_router(r)


def main():
    import uvicorn

    uvicorn.run("korvo_server.main:app", host="0.0.0.0", port=PORT, reload=False)


if __name__ == "__main__":
    main()
