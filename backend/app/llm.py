"""Model selection.

MODEL is a pydantic-ai model string, "<provider>:<model-id>". Each provider SDK reads its own
key from the environment (loaded from .env in app.config), so adding Claude, Google AI Studio
or an OpenAI-compatible endpoint (Ollama, vLLM...) is configuration only:

    groq:openai/gpt-oss-120b        GROQ_API_KEY
    anthropic:claude-sonnet-5-5     ANTHROPIC_API_KEY
    google-gla:gemini-2.5-flash     GOOGLE_API_KEY
    openai:qwen3:14b                OPENAI_API_KEY + OPENAI_BASE_URL=http://localhost:11434/v1
    oxion:<model-id>                OXION_API_KEY (+ optional OXION_BASE_URL)
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_ai import UserError
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from app.config import get_settings

# Groq's free tier allows ~8k tokens/minute; a capture plus fact-check needs several minutes'
# worth, so let the SDK keep waiting out 429s (it honours the retry-after the API sends).
GROQ_MAX_RETRIES = 10


def model_name(override: str | None = None) -> str:
    return override or get_settings().model


@lru_cache
def _groq_model(model_id: str) -> Model:
    from groq import AsyncGroq
    from pydantic_ai.models.groq import GroqModel
    from pydantic_ai.providers.groq import GroqProvider
    return GroqModel(model_id, provider=GroqProvider(groq_client=AsyncGroq(max_retries=GROQ_MAX_RETRIES)))


OXION_DEFAULT_BASE_URL = "https://www.oxionai.com/v1"


@lru_cache
def _oxion_model(model_id: str, api_key: str, base_url: str) -> Model:
    """Oxion AI speaks the OpenAI chat-completions API; it gets its own key and base URL so
    the OPENAI_* variables stay free for OpenAI itself or a local Ollama."""
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    return OpenAIChatModel(model_id, provider=OpenAIProvider(base_url=base_url, api_key=api_key))


def get_model(override: str | None = None) -> Model | str:
    """The model to run with: a configured Model for providers that need one, else the string."""
    name = model_name(override)
    if name.startswith("groq:"):
        return _groq_model(name.removeprefix("groq:"))
    if name.startswith("oxion:"):
        key = os.environ.get("OXION_API_KEY", "")
        if not key:
            raise UserError("Set OXION_API_KEY in .env to use an oxion: model.")
        base = os.environ.get("OXION_BASE_URL") or OXION_DEFAULT_BASE_URL
        return _oxion_model(name.removeprefix("oxion:"), key, base)
    return name


def model_settings() -> ModelSettings:
    # Low temperature: the agents edit a knowledge base and cite records, not write fiction.
    return ModelSettings(temperature=0.2, max_tokens=8192)
