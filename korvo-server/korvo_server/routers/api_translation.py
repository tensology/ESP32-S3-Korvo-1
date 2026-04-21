import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["translation"])


class TranslateBody(BaseModel):
    text: str
    source_language: str = "en"
    target_language: str = "ja"


@router.post("/translate/google")
async def translate_google(body: TranslateBody):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Text is required")

    source = (body.source_language or "en").strip().lower() or "en"
    target = (body.target_language or "ja").strip().lower() or "ja"
    if len(source) > 12 or len(target) > 12:
        raise HTTPException(400, "Invalid language code")

    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": source,
        "tl": target,
        "dt": "t",
        "q": text,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Google Translate HTTP error: {e.response.status_code}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Translation request failed: {e}") from e

    translated = ""
    try:
        parts = data[0] or []
        translated = "".join((p[0] or "") for p in parts if isinstance(p, list) and p)
    except Exception:  # noqa: BLE001
        translated = ""

    if not translated:
        raise HTTPException(502, "No translation returned")

    return {
        "translated_text": translated,
        "source_language": source,
        "target_language": target,
    }
