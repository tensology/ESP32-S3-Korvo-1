import json
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Query

from korvo_server.config import BIN_DIR

router = APIRouter(prefix="/api", tags=["networks"])


@router.get("/networks")
def list_networks(
    source: str = Query("all", description="'mac', 'esp32', or 'all'"),
    esp_host: str = Query("192.168.4.1"),
):
    networks: list[dict] = []
    seen: set[str] = set()

    if source in ("all", "mac"):
        app_bin = BIN_DIR / "KorvoWiFiScanner.app" / "Contents" / "MacOS" / "wifi_scan"
        plain_bin = BIN_DIR / "wifi_scan"
        scan_bin = app_bin if app_bin.is_file() else (plain_bin if plain_bin.is_file() else None)
        if scan_bin:
            try:
                out = subprocess.run(
                    [str(scan_bin)],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                for line in (out.stdout or "").strip().splitlines():
                    if not line or line.startswith("ERROR"):
                        continue
                    parts = line.split("|")
                    if len(parts) >= 2 and parts[0] and parts[1]:
                        typ, ssid = parts[0], parts[1]
                        try:
                            rssi = int(parts[2]) if len(parts) > 2 else -50
                        except ValueError:
                            rssi = -50
                        security = parts[3] if len(parts) > 3 else "Unknown"
                        if ssid and ssid not in seen:
                            seen.add(ssid)
                            networks.append(
                                {
                                    "ssid": ssid,
                                    "rssi": rssi,
                                    "security": "Connected" if typ == "CURRENT" else security,
                                    "source": "mac",
                                    "current": typ == "CURRENT",
                                }
                            )
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass

        if not networks:
            airport = Path(
                "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
            )
            if airport.is_file():
                try:
                    out = subprocess.run([str(airport), "-s"], capture_output=True, text=True, timeout=5)
                    lines = (out.stdout or "").strip().splitlines()[1:]
                    for line in lines:
                        m = re.match(r"^\s*(.+?)\s+([0-9a-f:]{17})\s+(-?\d+)\s+(-?\d+)\s+(\w+)\s+(.+)?", line)
                        if m:
                            ssid = m.group(1).strip()
                            rssi = int(m.group(3))
                            security = (m.group(6) or "").strip()
                            if ssid and ssid not in seen:
                                seen.add(ssid)
                                networks.append({"ssid": ssid, "rssi": rssi, "security": security, "source": "mac"})
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    pass

    if source in ("all", "esp32"):
        try:
            out = subprocess.run(
                ["curl", "-s", "--connect-timeout", "1", "--max-time", "2", f"http://{esp_host}/scan"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            esp_networks = json.loads(out.stdout or "[]")
            if isinstance(esp_networks, list):
                for n in esp_networks:
                    ssid = n.get("ssid")
                    if ssid and ssid not in seen:
                        seen.add(ssid)
                        networks.append({**n, "source": "esp32"})
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass

    networks.sort(key=lambda x: x.get("rssi", -999), reverse=True)
    note = (
        None
        if networks
        else "macOS redacts SSIDs on modern versions. Enter your SSID manually, or use the ESP32 to scan (connect to its AP at 192.168.4.1)"
    )
    return {"networks": networks, "source": "mac" if networks else "none", "note": note}
