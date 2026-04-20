from typing import Any

from fastapi import APIRouter

from korvo_server.config_gen import generate_config, has_active_wifi, read_config
from korvo_server.deps import KorvoDep

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings")
def get_settings(kdb: KorvoDep):
    with kdb.lock:
        rows = kdb.conn.execute("SELECT key, value, updated_at FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@router.post("/settings")
def post_settings(body: dict[str, Any], kdb: KorvoDep):
    with kdb.lock:
        for key, value in body.items():
            kdb.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (key, str(value)),
            )
        kdb.conn.commit()
        generate_config(kdb.conn, preserve_wifi_if_missing=not has_active_wifi(kdb.conn))
    return {"success": True}


@router.get("/config")
def get_config(kdb: KorvoDep):
    with kdb.lock:
        return read_config(kdb.conn)
