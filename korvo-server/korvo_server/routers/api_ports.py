import re
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["ports"])

_SKIP = re.compile(r"^(Bluetooth|debug-console|JBL|GroundControl|AVT)", re.I)


def _rank(name: str) -> int:
    s = name.lower()
    if "usbserial" in s:
        return 0
    if "wchusb" in s or "usbmodem" in s:
        return 1
    if "slab" in s:
        return 2
    return 5


@router.get("/ports")
def serial_ports():
    try:
        dev = Path("/dev")
        names = sorted(
            [p.name for p in dev.iterdir() if p.name.startswith("cu.")],
            key=lambda n: (_rank(n), n),
        )
        ports = []
        for f in names:
            rest = f[3:]
            if _SKIP.match(rest):
                continue
            if re.search(r"usb|serial|SLAB|wch|modem|acm", rest, re.I):
                ports.append(f"/dev/{f}")
        return {"ports": ports}
    except OSError as err:
        return {"ports": [], "error": str(err)}
