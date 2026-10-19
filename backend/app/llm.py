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

from pydantic_ai.settings import ModelSettings

from app.config import get_settings


def model_name(override: str | None = None) -> str:
    return override or get_settings().model


def model_settings() -> ModelSettings:
    # Low temperature: the agents edit a knowledge base and cite records, not write fiction.
    return ModelSettings(temperature=0.2, max_tokens=8192)
