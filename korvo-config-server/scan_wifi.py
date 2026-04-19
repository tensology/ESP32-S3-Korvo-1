#!/usr/bin/env python3
"""Scan WiFi networks using CoreWLAN (macOS native API)."""
import sys

try:
    from CoreWLAN import CWInterface, CWNetwork
    
    iface = CWInterface.interfaceWithName_("en0")
    if iface is None:
        iface = CWInterface.interfaceWithName_("en1")
    
    if iface:
        networks, error = iface.scanForNetworksWithName_error_(None, None)
        if networks:
            seen = set()
            for net in networks:
                ssid = net.ssid()
                if ssid and ssid not in seen:
                    seen.add(ssid)
                    rssi = net.rssiValue()
                    sec = str(net.securityType())
                    print(f"{ssid}|{rssi}|{sec}")
            sys.exit(0)
except ImportError:
    pass
except Exception as e:
    pass

# Fallback: try networksetup
try:
    import subprocess
    result = subprocess.run(
        ["networksetup", "-getairportnetwork", "en0"],
        capture_output=True, text=True, timeout=5
    )
    match = result.stdout.strip()
    if "Current Wi-Fi Network:" in match:
        ssid = match.replace("Current Wi-Fi Network:", "").strip()
        if ssid:
            print(f"{ssid}|-50|Connected")
except Exception:
    pass

sys.exit(0)