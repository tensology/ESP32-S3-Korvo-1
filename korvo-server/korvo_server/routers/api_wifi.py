from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import json
import urllib.error
import urllib.request
import socket
import subprocess
import threading
import time
import re

from korvo_server.config_gen import generate_config
from korvo_server.deps import KorvoDep

router = APIRouter(tags=["wifi"])


class WifiBody(BaseModel):
    ssid: str
    password: str = ""
    set_active: bool = False


def _normalize_board_host(raw: str | None) -> str:
    host = (raw or "").strip()
    if host.startswith("http://"):
        host = host[7:]
    elif host.startswith("https://"):
        host = host[8:]
    if "/" in host:
        host = host.split("/", 1)[0]
    return host.strip()


def _is_ipv4_host(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit():
            return False
        n = int(p)
        if n < 0 or n > 255:
            return False
    return True


def _candidate_board_hosts(settings: dict[str, str], include_neighbors: bool = False) -> list[str]:
    primary = _normalize_board_host(settings.get("esp_ip"))
    last_host = _normalize_board_host(settings.get("last_board_host"))
    last_sta_ip = _normalize_board_host(settings.get("last_board_sta_ip"))
    hosts: list[str] = []

    def _add(v: str):
        if v and v not in hosts:
            hosts.append(v)

    _add(primary)
    _add(last_host)
    _add(last_sta_ip)
    explicit = [primary, last_host, last_sta_ip]
    has_explicit_ipv4 = any(_is_ipv4_host(h) for h in explicit if h)
    has_explicit_mdns = any(h.endswith(".local") for h in explicit if h)
    # mDNS lookup can block for seconds when unavailable; only probe when needed.
    if has_explicit_mdns or not has_explicit_ipv4:
        _add("korvo.local")
    if include_neighbors:
        for ip in _arp_ipv4_candidates(limit=12):
            _add(ip)
    return hosts


def _arp_ipv4_candidates(limit: int = 48) -> list[str]:
    try:
        out = subprocess.run(
            ["arp", "-an"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    txt = (out.stdout or "").strip()
    if not txt:
        return []
    ips: list[str] = []
    for m in re.finditer(r"\((\d+\.\d+\.\d+\.\d+)\)", txt):
        ip = m.group(1)
        if not _is_ipv4_host(ip):
            continue
        # Skip loopback/link-local/multicast/broadcast ranges.
        if ip.startswith("127.") or ip.startswith("169.254.") or ip.startswith("224.") or ip == "255.255.255.255":
            continue
        if ip not in ips:
            ips.append(ip)
        if len(ips) >= max(1, int(limit)):
            break
    return ips


def _local_wifi_device() -> str | None:
    try:
        out = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    lines = (out.stdout or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() != "hardware port: wi-fi":
            continue
        for next_line in lines[i + 1 : i + 4]:
            if next_line.strip().lower().startswith("device:"):
                dev = next_line.split(":", 1)[1].strip()
                if dev:
                    return dev
    return None


def _local_wifi_ssid() -> str:
    dev = _local_wifi_device()
    if not dev:
        return ""
    try:
        out = subprocess.run(
            ["networksetup", "-getairportnetwork", dev],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    txt = (out.stdout or "").strip()
    if ":" in txt:
        return txt.split(":", 1)[1].strip()
    return ""


def _internet_reachable() -> bool:
    try:
        sock = socket.create_connection(("1.1.1.1", 53), timeout=2)
        sock.close()
        return True
    except OSError:
        return False


def _collect_wifi_profiles(conn) -> tuple[list[dict], int]:
    rows = conn.execute(
        """
        SELECT id, ssid, password, is_active
        FROM wifi_networks
        ORDER BY is_active DESC, COALESCE(updated_at, created_at) DESC, created_at DESC
        LIMIT 5
        """
    ).fetchall()
    profiles: list[dict] = []
    active_idx = 0
    for i, row in enumerate(rows):
        profiles.append(
            {
                "ssid": row["ssid"],
                "password": row["password"] or "",
                "is_active": bool(row["is_active"]),
            }
        )
        if row["is_active"]:
            active_idx = i
    return profiles, active_idx


def _prepare_board_sync_payload(conn) -> tuple[list[str], bytes] | None:
    profiles, active_idx = _collect_wifi_profiles(conn)
    settings_rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in settings_rows}
    hosts = _candidate_board_hosts(settings)
    if not hosts:
        return None
    if not profiles:
        return None
    payload = json.dumps({"networks": profiles, "active_index": active_idx}).encode("utf-8")
    return hosts, payload


def _sync_wifi_profiles_to_hosts(hosts: list[str], payload: bytes) -> dict:
    last_err: dict | None = None
    for host in hosts:
        req = urllib.request.Request(
            f"http://{host}/api/network/profiles",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                body = (resp.read() or b"").decode("utf-8", errors="replace")
                parsed = json.loads(body) if body else {}
                return {
                    "ok": bool(parsed.get("ok", False)),
                    "host": host,
                    "status": getattr(resp, "status", None),
                    "response": parsed,
                }
        except urllib.error.HTTPError as e:
            last_err = {"ok": False, "host": host, "reason": "http_error", "status": e.code}
        except (urllib.error.URLError, TimeoutError, ValueError):
            last_err = {"ok": False, "host": host, "reason": "unreachable"}
    return last_err or {"ok": False, "reason": "unreachable"}


def _sync_wifi_profiles_to_board(conn) -> dict:
    prepared = _prepare_board_sync_payload(conn)
    if not prepared:
        return {"ok": False, "reason": "missing_host_or_profiles"}
    hosts, payload = prepared
    return _sync_wifi_profiles_to_hosts(hosts, payload)


def _sync_wifi_profiles_to_board_async(conn) -> dict:
    prepared = _prepare_board_sync_payload(conn)
    if not prepared:
        return {"ok": False, "queued": False, "reason": "missing_host_or_profiles"}
    hosts, payload = prepared

    def _worker(h: list[str], p: bytes):
        try:
            _sync_wifi_profiles_to_hosts(h, p)
        except Exception:
            # Background sync best-effort by design.
            pass

    t = threading.Thread(target=_worker, args=(hosts, payload), daemon=True)
    t.start()
    return {"ok": True, "queued": True, "reason": "background_sync_started"}


def _post_board_led(host: str, payload: dict, timeout: float = 2.5) -> dict:
    req = urllib.request.Request(
        f"http://{host}/api/led",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
        parsed = json.loads(body) if body else {}
        return {
            "ok": True,
            "status": getattr(resp, "status", None),
            "response": parsed,
        }


def _find_reachable_board_host(hosts: list[str]) -> str | None:
    host, _, _ = _find_reachable_board_status(hosts)
    return host


def _looks_like_korvo_network_status(parsed: dict | None) -> bool:
    if not isinstance(parsed, dict):
        return False
    keys = set(parsed.keys())
    # Korvo firmware network status shape (allow evolution, require strong signal).
    if "sta_connected" in keys and ("sta_ip" in keys or "sta_ssid" in keys):
        return True
    if "profile_count" in keys and ("sta_connected" in keys or "sta_ip" in keys):
        return True
    return False


def _find_reachable_board_status(hosts: list[str]) -> tuple[str | None, dict | None, dict]:
    last: dict = {"ok": False, "reason": "unreachable"}
    for idx, host in enumerate(hosts):
        # First candidates are explicit (esp_ip / cache / mDNS), allow normal timeout.
        # ARP fallback candidates should fail fast to keep API responsive.
        timeout_sec = 1.2 if idx < 3 else 0.45
        req = urllib.request.Request(
            f"http://{host}/api/network/status",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                body = (resp.read() or b"").decode("utf-8", errors="replace")
                parsed = json.loads(body) if body else {}
                if not _looks_like_korvo_network_status(parsed):
                    last = {"ok": False, "host": host, "reason": "unexpected_payload"}
                    continue
                return host, parsed, {"ok": True, "host": host}
        except urllib.error.HTTPError as e:
            last = {"ok": False, "host": host, "reason": "http_error", "status": e.code}
        except (urllib.error.URLError, TimeoutError, ValueError):
            last = {"ok": False, "host": host, "reason": "unreachable"}
            continue
    return None, None, last


def _run_test_board_led_cue(host: str):
    # Rainbow palette for preset 11 (per-pixel fade); then turn LEDs off.
    rainbow = [
        [255, 0, 0],
        [255, 127, 0],
        [255, 220, 0],
        [120, 255, 0],
        [0, 255, 80],
        [0, 255, 255],
        [0, 140, 255],
        [0, 64, 255],
        [70, 0, 255],
        [140, 0, 255],
        [220, 0, 255],
        [255, 0, 140],
    ]
    start_payload = {
        "on": True,
        "preset": 11,
        "brightness": 56,
        "pixels": rainbow,
    }
    off_payload = {
        "on": False,
        "preset": 0,
    }
    try:
        _post_board_led(host, start_payload, timeout=3.0)
        time.sleep(2.8)
        _post_board_led(host, off_payload, timeout=2.0)
    except Exception:
        # Cue is best-effort and must not break the main API.
        return


@router.get("/api/wifi")
def wifi_list(kdb: KorvoDep):
    with kdb.lock:
        cur = kdb.conn.cursor()
        networks = cur.execute(
            "SELECT id, ssid, is_active, created_at, updated_at FROM wifi_networks "
            "ORDER BY COALESCE(updated_at, created_at) DESC, created_at DESC"
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
            sync = _sync_wifi_profiles_to_board_async(kdb.conn)
            return {"success": True, "id": row["id"], "updated": True, "board_sync": sync}
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
        sync = _sync_wifi_profiles_to_board_async(kdb.conn)
        return {"success": True, "id": rid, "board_sync": sync}


@router.delete("/api/wifi/{net_id}")
def wifi_delete(net_id: int, kdb: KorvoDep):
    with kdb.lock:
        kdb.conn.execute("DELETE FROM wifi_networks WHERE id = ?", (net_id,))
        kdb.conn.commit()
        generate_config(kdb.conn)
        sync = _sync_wifi_profiles_to_board_async(kdb.conn)
    return {"success": True, "board_sync": sync}


@router.post("/api/wifi/{net_id}/activate")
def wifi_activate(net_id: int, kdb: KorvoDep):
    with kdb.lock:
        cur = kdb.conn.cursor()
        cur.execute("UPDATE wifi_networks SET is_active = 0")
        cur.execute("UPDATE wifi_networks SET is_active = 1 WHERE id = ?", (net_id,))
        kdb.conn.commit()
        generate_config(kdb.conn)
        sync = _sync_wifi_profiles_to_board_async(kdb.conn)
    return {"success": True, "board_sync": sync}


@router.post("/api/wifi/sync")
def wifi_sync_now(kdb: KorvoDep):
    with kdb.lock:
        sync = _sync_wifi_profiles_to_board(kdb.conn)
    return {"success": bool(sync.get("ok")), "board_sync": sync}


@router.get("/api/wifi/runtime")
def wifi_runtime_status(kdb: KorvoDep):
    with kdb.lock:
        settings_rows = kdb.conn.execute("SELECT key, value FROM settings").fetchall()
        settings = {r["key"]: r["value"] for r in settings_rows}
        hosts = _candidate_board_hosts(settings, include_neighbors=False)
        if not hosts:
            return {"ok": False, "reason": "missing_esp_ip"}
        host, parsed, last = _find_reachable_board_status(hosts)
        if not host or parsed is None:
            return last
        sta_ip = _normalize_board_host(parsed.get("sta_ip"))
        persisted_host = sta_ip if _is_ipv4_host(sta_ip) else host
        updates: list[tuple[str, str]] = []
        if persisted_host and settings.get("esp_ip") != persisted_host:
            updates.append(("esp_ip", persisted_host))
        if settings.get("last_board_host") != host:
            updates.append(("last_board_host", host))
        if _is_ipv4_host(sta_ip) and settings.get("last_board_sta_ip") != sta_ip:
            updates.append(("last_board_sta_ip", sta_ip))
        if updates:
            for key, value in updates:
                kdb.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (key, value),
                )
            kdb.conn.commit()
        return {"ok": True, "host": host, "status": parsed}


@router.post("/api/wifi/test-board-cue")
def wifi_test_board_cue(kdb: KorvoDep):
    with kdb.lock:
        settings_rows = kdb.conn.execute("SELECT key, value FROM settings").fetchall()
        settings = {r["key"]: r["value"] for r in settings_rows}
    hosts = _candidate_board_hosts(settings, include_neighbors=True)
    host = _find_reachable_board_host(hosts)
    if not host:
        return {"ok": False, "reason": "unreachable", "hosts": hosts}
    t = threading.Thread(target=_run_test_board_led_cue, args=(host,), daemon=True)
    t.start()
    return {"ok": True, "host": host, "queued": True}


@router.get("/api/wifi/test-local")
def wifi_test_local(ssid: str = Query("", description="Optional SSID to compare with current local SSID")):
    current = _local_wifi_ssid()
    target = (ssid or "").strip()
    on_target = bool(target and current and target == current)
    return {
        "ok": True,
        "machine": "local",
        "current_ssid": current,
        "target_ssid": target,
        "on_target_ssid": on_target,
        "internet_reachable": _internet_reachable(),
    }
