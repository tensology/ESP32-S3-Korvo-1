import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from korvo_server.routers.api_tts import _polly_client

router = APIRouter(prefix="/api/vendors", tags=["vendors"])


class VendorValidateBody(BaseModel):
    vendor: str
    api_key: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "eu-west-1"
    aws_session_token: str = ""


@router.post("/validate")
async def validate_vendor(body: VendorValidateBody):
    vendor = (body.vendor or "").strip().lower()
    if vendor in {"assemblyai", "openai", "anthropic", "gemini", "elevenlabs"}:
        key = (body.api_key or "").strip()
        if not key:
            raise HTTPException(400, "API key is required")

    try:
        if vendor == "assemblyai":
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get("https://api.assemblyai.com/v2/account", headers={"Authorization": key})
            if r.status_code != 200:
                raise HTTPException(400, "Invalid AssemblyAI key")
            return {"valid": True, "vendor": vendor}

        if vendor == "openai":
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                raise HTTPException(400, "Invalid OpenAI key")
            return {"valid": True, "vendor": vendor}

        if vendor == "anthropic":
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
            if r.status_code != 200:
                raise HTTPException(400, "Invalid Anthropic key")
            return {"valid": True, "vendor": vendor}

        if vendor == "gemini":
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key})
            if r.status_code != 200:
                raise HTTPException(400, "Invalid Google Gemini key")
            return {"valid": True, "vendor": vendor}

        if vendor == "elevenlabs":
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key})
            if r.status_code != 200:
                raise HTTPException(400, "Invalid ElevenLabs key")
            return {"valid": True, "vendor": vendor}

        if vendor == "aws_polly":
            ak = (body.aws_access_key_id or "").strip()
            sk = (body.aws_secret_access_key or "").strip()
            region = (body.aws_region or "eu-west-1").strip()
            st = (body.aws_session_token or "").strip()
            if not ak or not sk:
                raise HTTPException(400, "AWS access key and secret are required")
            client = _polly_client(ak, sk, region, st)
            client.describe_voices(MaxResults=1)
            return {"valid": True, "vendor": vendor, "region": region}

        raise HTTPException(400, "Unsupported vendor")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Validation failed: {e}") from e
