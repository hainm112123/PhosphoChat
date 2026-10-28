"""Model selection for every supported provider.

A model is "<prefix>:<model-id>". The PROVIDERS table below lists what each prefix needs; it
drives model creation, the startup report and the docs.

    groq:openai/gpt-oss-120b          GROQ_API_KEY
    anthropic:claude-sonnet-5-5       ANTHROPIC_API_KEY          (alias claude:)
    google:gemini-2.5-flash           GOOGLE_API_KEY             (aliases gemini:, google-gla:)
    google-cloud:gemini-2.5-flash     GOOGLE_CLOUD_PROJECT (+ credentials; alias google-vertex:)
    openai:gpt-5.2                    OPENAI_API_KEY             (Responses API)
    openai-chat:gpt-5.2               OPENAI_API_KEY             (Chat Completions API)
    openrouter:<vendor>/<model>       OPENROUTER_API_KEY
    ollama:qwen3:14b                  OLLAMA_BASE_URL (default http://localhost:11434/v1)
    lmstudio:<model-id>               LMSTUDIO_BASE_URL (default http://localhost:1234/v1)
    vllm:<model-id>                   VLLM_BASE_URL (default http://localhost:8000/v1)
    compat:<name>:<model-id>          COMPAT_<NAME>_BASE_URL (+ optional COMPAT_<NAME>_API_KEY)
    oxion:<model-id>                  OXION_API_KEY (+ optional OXION_BASE_URL)

Any other pydantic-ai prefix (mistral:, deepseek:, ...) is passed through unchanged.
CHAT_MODEL, JOB_MODEL and VERIFY_MODEL optionally pick a different model per role.
Inside Docker, local servers default to host.docker.internal instead of localhost.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_ai import UserError
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from app.config import get_settings

# Groq's free tier allows ~8k tokens/minute; a capture plus fact-check needs several minutes'
# worth, so let the SDK keep waiting out 429s (it honours the retry-after the API sends).
GROQ_MAX_RETRIES = 10

Role = Literal["chat", "job", "verify"]
Kind = Literal["cloud", "local", "compat", "passthrough"]


@dataclass(frozen=True)
class Provider:
    prefix: str
    label: str
    kind: Kind
    keys: tuple[str, ...] = ()        # needs at least one of these (cloud) / optional (local)
    base_url_env: str | None = None
    default_url: str | None = None


PROVIDERS: dict[str, Provider] = {p.prefix: p for p in [
    Provider("groq", "Groq", "cloud", ("GROQ_API_KEY",)),
    Provider("anthropic", "Claude (Anthropic)", "cloud", ("ANTHROPIC_API_KEY",)),
    Provider("google", "Gemini (Google AI Studio)", "cloud", ("GOOGLE_API_KEY", "GEMINI_API_KEY")),
    Provider("google-cloud", "Gemini (Vertex AI)", "cloud", ("GOOGLE_CLOUD_PROJECT",)),
    Provider("openai", "OpenAI (Responses API)", "cloud", ("OPENAI_API_KEY",)),
    Provider("openai-chat", "OpenAI (Chat Completions)", "cloud", ("OPENAI_API_KEY",)),
    Provider("openai-responses", "OpenAI (Responses API)", "cloud", ("OPENAI_API_KEY",)),
    Provider("openrouter", "OpenRouter", "cloud", ("OPENROUTER_API_KEY",)),
    Provider("ollama", "Ollama (local)", "local", ("OLLAMA_API_KEY",), "OLLAMA_BASE_URL",
             "http://localhost:11434/v1"),
    Provider("lmstudio", "LM Studio (local)", "local", ("LMSTUDIO_API_KEY",), "LMSTUDIO_BASE_URL",
             "http://localhost:1234/v1"),
    Provider("vllm", "vLLM / llama.cpp server", "local", ("VLLM_API_KEY",), "VLLM_BASE_URL",
             "http://localhost:8000/v1"),
    Provider("oxion", "Oxion AI", "compat", ("OXION_API_KEY",), "OXION_BASE_URL",
             "https://www.oxionai.com/v1"),
    Provider("compat", "OpenAI-compatible endpoint", "compat"),
]}

ALIASES = {"claude": "anthropic", "gemini": "google", "google-gla": "google",
           "google-vertex": "google-cloud"}

OXION_DEFAULT_BASE_URL = PROVIDERS["oxion"].default_url


# --------------------------------------------------------------------------- roles

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


# --------------------------------------------------------------------------- resolution

def split_name(name: str) -> tuple[str, str]:
    """'claude:claude-sonnet-5-5' -> ('anthropic', 'claude-sonnet-5-5'), aliases applied."""
    prefix, sep, model_id = name.partition(":")
    if not sep or not model_id:
        raise UserError(f"Model '{name}' needs the form <provider>:<model-id>, e.g. groq:openai/gpt-oss-120b.")
    prefix = prefix.strip().lower()
    return ALIASES.get(prefix, prefix), model_id.strip()


def in_docker() -> bool:
    return Path("/.dockerenv").exists()


def _local_default(url: str) -> str:
    """Inside a container, 'localhost' is the container: reach the host's server instead."""
    return url.replace("localhost", "host.docker.internal") if in_docker() else url


def _env(name: str | None) -> str:
    return os.environ.get(name, "").strip() if name else ""


def provider_base_url(p: Provider) -> str | None:
    if p.kind == "local":
        return _env(p.base_url_env) or _local_default(p.default_url or "")
    if p.kind == "compat" and p.base_url_env:
        return _env(p.base_url_env) or p.default_url
    return None


def compat_endpoint(model_id: str) -> tuple[str, str, str, str | None]:
    """'local:qwen3:14b' -> ('local', 'qwen3:14b', base_url, api_key) from COMPAT_LOCAL_*."""
    name, sep, inner = model_id.partition(":")
    if not sep or not inner or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise UserError("Use compat:<endpoint>:<model-id>, e.g. compat:local:qwen3:14b, with "
                        "COMPAT_LOCAL_BASE_URL set in .env.")
    env = f"COMPAT_{name.upper()}"
    base = _env(f"{env}_BASE_URL")
    if not base:
        known = sorted({k[7:-9].lower() for k in os.environ
                        if k.startswith("COMPAT_") and k.endswith("_BASE_URL")})
        raise UserError(f"Set {env}_BASE_URL in .env for compat:{name}:... models."
                        + (f" Configured endpoints: {', '.join(known)}." if known else ""))
    return name.lower(), inner, base, _env(f"{env}_API_KEY") or None


@lru_cache(maxsize=64)
def _build(prefix: str, model_id: str, base_url: str | None, api_key: str | None) -> Model:
    if prefix == "groq":
        from groq import AsyncGroq
        from pydantic_ai.models.groq import GroqModel
        from pydantic_ai.providers.groq import GroqProvider
        return GroqModel(model_id, provider=GroqProvider(groq_client=AsyncGroq(max_retries=GROQ_MAX_RETRIES)))
    if prefix == "ollama":
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider
        return OllamaModel(model_id, provider=OllamaProvider(base_url=base_url, api_key=api_key or "ollama"))
    from pydantic_ai.models.openai import OpenAIChatModel
    if prefix == "vllm":
        from pydantic_ai.providers.vllm import VLLMProvider
        return OpenAIChatModel(model_id, provider=VLLMProvider(base_url=base_url, api_key=api_key or "vllm"))
    # lmstudio, oxion, compat: plain OpenAI chat-completions against their own base URL/key.
    from pydantic_ai.providers.openai import OpenAIProvider
    return OpenAIChatModel(model_id, provider=OpenAIProvider(base_url=base_url, api_key=api_key or "not-needed"))


def get_model(override: str | None = None, role: Role = "chat") -> Model | str:
    """The model to run with: a configured Model for providers that need one, else the
    pydantic-ai model string (cloud providers read their own keys from the environment)."""
    prefix, model_id = split_name(model_name(override, role))
    p = PROVIDERS.get(prefix)
    if prefix == "groq":
        return _build("groq", model_id, None, None)
    if prefix == "compat":
        _, inner, base, key = compat_endpoint(model_id)
        return _build("compat", inner, base, key)
    if p and p.kind in ("local", "compat"):
        key = _env(p.keys[0]) if p.keys else ""
        if p.kind == "compat" and not key:
            raise UserError(f"Set {p.keys[0]} in .env to use {prefix}: models.")
        return _build(prefix, model_id, provider_base_url(p), key or None)
    return f"{prefix}:{model_id}"  # cloud and pass-through: pydantic-ai builds them


# --------------------------------------------------------------------------- status

def check_model(name: str) -> dict:
    """What a model needs and whether it's configured. Never includes secret values."""
    try:
        prefix, model_id = split_name(name)
    except UserError as e:
        return {"model": name, "provider": "?", "status": "invalid", "detail": str(e)}
    p = PROVIDERS.get(prefix)
    if p is None:
        return {"model": name, "provider": prefix, "status": "passthrough",
                "detail": "Handled by pydantic-ai directly; it reads its own key variable."}
    out = {"model": name, "provider": p.label}
    if prefix == "compat":
        try:
            endpoint, _, base, key = compat_endpoint(model_id)
            return {**out, "status": "ok", "detail": f"endpoint '{endpoint}' at {base}"
                    + ("" if key else " (no API key set)")}
        except UserError as e:
            return {**out, "status": "missing", "detail": str(e)}
    if p.kind == "local":
        return {**out, "status": "local", "detail": f"local server at {provider_base_url(p)}"}
    if not any(_env(k) for k in p.keys):
        return {**out, "status": "missing", "detail": f"set {' or '.join(p.keys)}"}
    if p.kind == "compat":
        return {**out, "status": "ok", "detail": f"key set, {provider_base_url(p)}"}
    return {**out, "status": "ok", "detail": "key set"}


def check_role_models() -> dict[str, dict]:
    return {role: check_model(name) for role, name in role_models().items()}


def connection_hint(e: BaseException) -> str | None:
    """A friendly message when a local/compat server can't be reached."""
    chain, seen = [], e
    while seen is not None and len(chain) < 10:
        chain.append(seen)
        seen = seen.__cause__ or seen.__context__
    if not any("connect" in type(x).__name__.lower() or "connection refused" in str(x).lower()
               or "connecterror" in repr(x).lower() for x in chain):
        return None
    url = next((str(getattr(getattr(x, "request", None), "url", "")) for x in chain
                if getattr(getattr(x, "request", None), "url", None)), "")
    msg = f"Can't reach the model server{f' at {url}' if url else ''}. Is it running?"
    if "11434" in url or "host.docker.internal" in url:
        msg += (" If Ollama runs as the desktop app and PhosphoChat in Docker, set "
                "OLLAMA_HOST=0.0.0.0 for the Ollama app and restart it.")
    return msg


def model_settings() -> ModelSettings:
    # Low temperature: the agents edit a knowledge base and cite records, not write fiction.
    return ModelSettings(temperature=0.2, max_tokens=8192)
