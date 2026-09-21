"""Cloud credentials stay in SQLite. GET responses do not echo them."""

SECRET_SETTING_KEYS = frozenset(
    {
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "assemblyai_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "google_gemini_api_key",
        "elevenlabs_api_key",
    }
)


def redact_settings(raw: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key in SECRET_SETTING_KEYS:
            out[key] = ""
            out[f"{key}_set"] = "1" if (value or "").strip() else "0"
        else:
            out[key] = value
    return out


def merge_secret_updates(existing: dict[str, str], incoming: dict[str, object]) -> dict[str, str]:
    """Empty secret fields keep the stored value. A non-empty value replaces it."""
    merged: dict[str, str] = {}
    for key, value in incoming.items():
        text = str(value)
        if key in SECRET_SETTING_KEYS and not text.strip():
            if key in existing:
                merged[key] = existing[key]
            continue
        merged[key] = text
    return merged
