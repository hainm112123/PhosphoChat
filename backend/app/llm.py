"""Model selection.

MODEL is a pydantic-ai model string, "<provider>:<model-id>". Each provider SDK reads its own
key from the environment (loaded from .env in app.config), so adding Claude, Google AI Studio
or an OpenAI-compatible endpoint (Ollama, vLLM...) is configuration only:

    groq:openai/gpt-oss-120b        GROQ_API_KEY
    anthropic:claude-sonnet-5-5     ANTHROPIC_API_KEY
    google-gla:gemini-2.5-flash     GOOGLE_API_KEY
    openai:qwen3:14b                OPENAI_API_KEY + OPENAI_BASE_URL=http://localhost:11434/v1
"""

from __future__ import annotations

from functools import lru_cache

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


def get_model(override: str | None = None) -> Model | str:
    """The model to run with: a configured Model for providers that need one, else the string."""
    name = model_name(override)
    if name.startswith("groq:"):
        return _groq_model(name.removeprefix("groq:"))
    return name


def model_settings() -> ModelSettings:
    # Low temperature: the agents edit a knowledge base and cite records, not write fiction.
    return ModelSettings(temperature=0.2, max_tokens=8192)
