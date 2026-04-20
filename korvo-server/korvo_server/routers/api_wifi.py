from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from korvo_server.config_gen import generate_config
from korvo_server.deps import KorvoDep

router = APIRouter(tags=["wifi"])


class WifiBody(BaseModel):
    ssid: str
    password: str = ""
    set_active: bool = False


@router.get("/api/wifi")
def wifi_list(kdb: KorvoDep):
    with kdb.lock:
        cur = kdb.conn.cursor()
        networks = cur.execute(
            "SELECT id, ssid, is_active, created_at FROM wifi_networks ORDER BY created_at DESC"
        ).fetchall()
        active = cur.execute("SELECT * FROM wifi_networks WHERE is_active = 1").fetchone()
    return {
        "networks": [dict(r) for r in networks],
        "active": dict(active) if active else None,
    }


@router.post("/api/wifi")
def wifi_upsert(body: WifiBody, kdb: KorvoDep):
    if not body.ssid:
        raise HTTPException(400, "SSID required")
    with kdb.lock:
        cur = kdb.conn.cursor()
        row = cur.execute("SELECT id FROM wifi_networks WHERE ssid = ?", (body.ssid,)).fetchone()
        any_active = cur.execute("SELECT 1 FROM wifi_networks WHERE is_active = 1 LIMIT 1").fetchone()
        if row:
            cur.execute(
                "UPDATE wifi_networks SET password = ?, updated_at = CURRENT_TIMESTAMP WHERE ssid = ?",
                (body.password or "", body.ssid),
            )
            if body.set_active:
                cur.execute("UPDATE wifi_networks SET is_active = 0")
                cur.execute("UPDATE wifi_networks SET is_active = 1 WHERE ssid = ?", (body.ssid,))
            kdb.conn.commit()
            generate_config(kdb.conn)
            return {"success": True, "id": row["id"], "updated": True}
        insert_active = 1 if (body.set_active or not any_active) else 0
        if body.set_active or insert_active:
            cur.execute("UPDATE wifi_networks SET is_active = 0")
        cur.execute(
            "INSERT INTO wifi_networks (ssid, password, is_active) VALUES (?, ?, ?)",
            (body.ssid, body.password or "", insert_active),
        )
        rid = cur.lastrowid
        kdb.conn.commit()
        generate_config(kdb.conn)
        return {"success": True, "id": rid}


@router.delete("/api/wifi/{net_id}")
def wifi_delete(net_id: int, kdb: KorvoDep):
    with kdb.lock:
        kdb.conn.execute("DELETE FROM wifi_networks WHERE id = ?", (net_id,))
        kdb.conn.commit()
        generate_config(kdb.conn)
    return {"success": True}


@router.post("/api/wifi/{net_id}/activate")
def wifi_activate(net_id: int, kdb: KorvoDep):
    with kdb.lock:
        cur = kdb.conn.cursor()
        cur.execute("UPDATE wifi_networks SET is_active = 0")
        cur.execute("UPDATE wifi_networks SET is_active = 1 WHERE id = ?", (net_id,))
        kdb.conn.commit()
        generate_config(kdb.conn)
    return {"success": True}
