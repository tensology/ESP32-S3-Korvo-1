from typing import Any

from fastapi import APIRouter

from korvo_server.config_gen import generate_config, has_active_wifi, read_config
from korvo_server.deps import KorvoDep
from korvo_server.secret_settings import merge_secret_updates, redact_settings

router = APIRouter(prefix="/api", tags=["settings"])


def _settings_map(kdb) -> dict[str, str]:
    rows = kdb.conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@router.get("/settings")
def get_settings(kdb: KorvoDep):
    with kdb.lock:
        return redact_settings(_settings_map(kdb))


@router.post("/settings")
def post_settings(body: dict[str, Any], kdb: KorvoDep):
    with kdb.lock:
        existing = _settings_map(kdb)
        merged = merge_secret_updates(existing, body)
        merged.pop("board_api_token", None)
        for key, value in merged.items():
            kdb.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (key, value),
            )
        kdb.conn.commit()
        generate_config(kdb.conn, preserve_wifi_if_missing=not has_active_wifi(kdb.conn))
    return {"success": True}


@router.get("/config")
def get_config(kdb: KorvoDep):
    with kdb.lock:
        config = read_config(kdb.conn)
    wifi = config.get("wifi")
    if isinstance(wifi, dict):
        public_wifi = {k: v for k, v in wifi.items() if k != "password"}
        public_wifi["password_set"] = bool((wifi.get("password") or "").strip())
        config["wifi"] = public_wifi
    settings = config.get("settings")
    if isinstance(settings, dict):
        config["settings"] = redact_settings(settings)
    return config
