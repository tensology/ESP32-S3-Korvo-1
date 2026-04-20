from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class ModelConfig:
    name: str
    provider: str
    endpoint: str
    headers: Dict[str, str]
    max_tokens: int


OPENROUTER_MODELS: Dict[str, ModelConfig] = {
    "qwen/qwen3.6-plus": ModelConfig(
        name="Qwen 3.6 Plus",
        provider="openrouter",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        headers={"HTTP-Referer": "https://korvosystem.com", "X-Title": "Korvo Voice Assistant"},
        max_tokens=8192,
    ),
    "qwen/qwen3.6-plus:free": ModelConfig(
        name="Qwen 3.6 Plus (Free)",
        provider="openrouter",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        headers={"HTTP-Referer": "https://korvosystem.com", "X-Title": "Korvo Voice Assistant"},
        max_tokens=8192,
    ),
}

DEFAULT_MODEL = "qwen/qwen3.6-plus"


def format_messages(transcript: str) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "You are a helpful, concise AI assistant integrated with a voice device. Respond naturally to voice interactions.",
        },
        {"role": "user", "content": transcript},
    ]


def model_payload(model_id: str, transcript: str) -> tuple[ModelConfig, Dict[str, Any]]:
    cfg = OPENROUTER_MODELS.get(model_id)
    if not cfg:
        raise ValueError("Invalid model")
    body = {
        "model": model_id,
        "messages": format_messages(transcript),
        "max_tokens": cfg.max_tokens,
    }
    return cfg, body
