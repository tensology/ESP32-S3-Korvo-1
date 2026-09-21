"""Shared board token. The dashboard (localhost) receives it; the board requires it after flash."""

_token = ""


def set_board_token(token: str) -> None:
    global _token
    _token = (token or "").strip()


def board_token() -> str:
    return _token


def board_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    if _token:
        headers["X-Korvo-Token"] = _token
    return headers
