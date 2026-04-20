import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from korvo_server.openrouter_models import DEFAULT_MODEL, OPENROUTER_MODELS, model_payload

router = APIRouter(prefix="/api", tags=["agent"])


class OpenRouterBody(BaseModel):
    transcript: str
    api_key: str
    model: str = DEFAULT_MODEL


@router.post("/agent/openrouter")
async def agent_openrouter(body: OpenRouterBody):
    if not body.transcript:
        raise HTTPException(400, "Transcript required")
    if not body.api_key:
        raise HTTPException(400, "API key required")
    try:
        cfg, payload = model_payload(body.model, body.transcript)
    except ValueError:
        raise HTTPException(400, "Invalid model") from None
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                cfg.endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {body.api_key}",
                    "Content-Type": "application/json",
                    **cfg.headers,
                },
            )
            r.raise_for_status()
            data = r.json()
        text = data["choices"][0]["message"]["content"]
        return {"response": text, "modelUsed": body.model}
    except httpx.HTTPStatusError as e:
        detail = e.response.text if e.response else str(e)
        raise HTTPException(500, f"OpenRouter HTTP error: {detail}") from e
    except Exception as e:
        raise HTTPException(500, f"Failed to get response from OpenRouter API: {e}") from e


@router.get("/models")
def list_models():
    return {
        "availableModels": {k: {"name": v.name, "provider": v.provider} for k, v in OPENROUTER_MODELS.items()},
        "defaultModel": DEFAULT_MODEL,
    }
