"""Model selection.

MODEL is a pydantic-ai model string, "<provider>:<model-id>". Each provider SDK reads its own
key from the environment (loaded from .env in app.config), so adding Claude, Google AI Studio
or an OpenAI-compatible endpoint (Ollama, vLLM...) is configuration only:

    groq:openai/gpt-oss-120b        GROQ_API_KEY
    anthropic:claude-sonnet-5-5     ANTHROPIC_API_KEY
    google-gla:gemini-2.5-flash     GOOGLE_API_KEY
    openai:qwen3:14b                OPENAI_API_KEY + OPENAI_BASE_URL=http://localhost:11434/v1
    oxion:<model-id>                OXION_API_KEY (+ optional OXION_BASE_URL)

CHAT_MODEL, JOB_MODEL and VERIFY_MODEL optionally pick a different model per role.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic_ai import UserError
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from app.config import get_settings

# Groq's free tier allows ~8k tokens/minute; a capture plus fact-check needs several minutes'
# worth, so let the SDK keep waiting out 429s (it honours the retry-after the API sends).
GROQ_MAX_RETRIES = 10


Role = Literal["chat", "job", "verify"]


def model_name(override: str | None = None, role: Role = "chat") -> str:
    """Resolve the model for a role: explicit override > role setting > fallback.

    CHAT_MODEL and JOB_MODEL fall back to MODEL; VERIFY_MODEL falls back to the job model.
    Jobs write the permanent wiki, so give JOB_MODEL your strongest model; on Groq, rate limits
    are per model, so separate roles also get separate token budgets."""
    if override:
        return override
    s = get_settings()
    job = s.job_model or s.model
    return {"chat": s.chat_model or s.model, "job": job, "verify": s.verify_model or job}[role]


def role_models() -> dict[str, str]:
    return {r: model_name(role=r) for r in ("chat", "job", "verify")}


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


def get_model(override: str | None = None, role: Role = "chat") -> Model | str:
    """The model to run with: a configured Model for providers that need one, else the string."""
    name = model_name(override, role)
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
