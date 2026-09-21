"""LAN host checks for board URLs and ESP scan targets.

Public 172.0.0.0/12 and 172.32.0.0/11 addresses used to pass a prefix check.
Only loopback, RFC1918, and names that resolve entirely inside those ranges pass.
"""

import ipaddress
import socket
from urllib.parse import urlparse


def _ip_is_local(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    return bool(ip.is_loopback or ip.is_private)


def host_allowed(host: str) -> bool:
    hn = (host or "").strip().lower().rstrip(".")
    if not hn or hn == "0.0.0.0" or "/" in hn or "@" in hn or " " in hn:
        return False
    if hn.startswith("[") or hn.endswith("."):
        return False
    try:
        return _ip_is_local(ipaddress.ip_address(hn))
    except ValueError:
        pass
    if len(hn) > 253:
        return False
    try:
        infos = socket.getaddrinfo(hn, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    seen = False
    for info in infos:
        addr = info[4][0]
        if addr.startswith("::ffff:"):
            addr = addr.split("::ffff:", 1)[1]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if not _ip_is_local(ip):
            return False
        seen = True
    return seen


def split_host_port(raw: str) -> str:
    text = (raw or "").strip()
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        if port.isdigit():
            return host
    return text


def url_allowed(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    return host_allowed(parsed.hostname or "")
