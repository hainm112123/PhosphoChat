"""Live list of the chat models each configured provider offers (for the admin Models page).

Every provider with a key (or a local server) is asked for its models in parallel; non-chat
models (speech, embeddings, guards, image...) are filtered out. Results are cached for a few
minutes. One provider failing never breaks the list: it just reports its error.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from app.llm import PROVIDERS, Provider, provider_base_url

CACHE_SECONDS = 300
TIMEOUT = 6.0

_cache: dict | None = None
_cache_at = 0.0
_transport: httpx.AsyncBaseTransport | None = None  # tests inject a MockTransport


@dataclass
class ProviderModels:
    prefix: str
    label: str
    status: str                       # ok | not_configured | error
    models: list[dict] = field(default_factory=list)
    error: str | None = None
    needs: str | None = None          # what to set when not configured

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _key(p: Provider) -> str:
    return next((_env(k) for k in p.keys if _env(k)), "")


# --------------------------------------------------------------------------- filters

# Speech, embeddings, guards, image/music generation and special-purpose agents (computer use,
# robotics) can't run PhosphoChat's text + tool-calling agents.
NOT_CHAT = re.compile(r"whisper|guard|orpheus|tts|embed|dall-e|image|imagen|audio|realtime|"
                      r"transcribe|moderation|search|aqa|playai|lyria|banana|veo|robotics|"
                      r"computer-use|antigravity", re.I)


def _groq(data: dict) -> list[str]:
    return [m["id"] for m in data.get("data", [])
            if m.get("active", True) and not NOT_CHAT.search(m["id"])]


def _openai(data: dict) -> list[str]:
    keep = re.compile(r"^(gpt-|o\d|chatgpt-)", re.I)
    return [m["id"] for m in data.get("data", []) if keep.match(m["id"]) and not NOT_CHAT.search(m["id"])]


def _anthropic(data: dict) -> list[str]:
    return [m["id"] for m in data.get("data", [])]


def _google(data: dict) -> list[str]:
    out = []
    for m in data.get("models", []):
        name = m.get("name", "").removeprefix("models/")
        if "generateContent" in m.get("supportedGenerationMethods", []) and not NOT_CHAT.search(name):
            out.append(name)
    return out


def _openrouter(data: dict) -> list[str]:
    return [m["id"] for m in data.get("data", []) if "tools" in (m.get("supported_parameters") or [])]


def _compatible(data: dict) -> list[str]:
    return [m["id"] for m in data.get("data", []) if not re.search(r"embed", m["id"], re.I)]


# --------------------------------------------------------------------------- requests

@dataclass
class Source:
    prefix: str                       # model id prefix, e.g. "groq" or "compat:local"
    label: str
    url: str
    parse: Callable[[dict], list[str]]
    headers: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    local: bool = False


def _sources() -> tuple[list[Source], list[ProviderModels]]:
    """Which providers to ask, and which are listed as not configured."""
    srcs, missing = [], []

    def need(p: Provider):
        missing.append(ProviderModels(p.prefix, p.label, "not_configured", needs=" or ".join(p.keys)))

    for prefix, url, parse, auth in [
        ("groq", "https://api.groq.com/openai/v1/models", _groq, "bearer"),
        ("openai", (_env("OPENAI_BASE_URL") or "https://api.openai.com/v1") + "/models", _openai, "bearer"),
        ("anthropic", "https://api.anthropic.com/v1/models?limit=100", _anthropic, "anthropic"),
        ("google", "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", _google, "query"),
        ("openrouter", "https://openrouter.ai/api/v1/models", _openrouter, "bearer"),
    ]:
        p = PROVIDERS[prefix]
        key = _key(p)
        if not key:
            need(p)
            continue
        headers, params = {}, {}
        if auth == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        elif auth == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        else:
            params["key"] = key
        srcs.append(Source(prefix, p.label, url, parse, headers, params))

    # Local servers: try Ollama and LM Studio at their usual ports (no key needed). vLLM's
    # default port (8000) is also PhosphoChat's, so it's only asked when VLLM_BASE_URL is set.
    for prefix in ("ollama", "lmstudio", "vllm"):
        p = PROVIDERS[prefix]
        if prefix == "vllm" and not _env("VLLM_BASE_URL"):
            missing.append(ProviderModels(prefix, p.label, "not_configured", needs="VLLM_BASE_URL"))
            continue
        base = provider_base_url(p) or ""
        key = _key(p)
        srcs.append(Source(prefix, p.label, base.rstrip("/") + "/models", _compatible,
                           {"Authorization": f"Bearer {key}"} if key else {}, local=True))

    # Oxion and every COMPAT_<NAME>_BASE_URL endpoint.
    ox = PROVIDERS["oxion"]
    if _key(ox):
        srcs.append(Source("oxion", ox.label, (provider_base_url(ox) or "").rstrip("/") + "/models",
                           _compatible, {"Authorization": f"Bearer {_key(ox)}"}))
    for var in sorted(os.environ):
        m = re.fullmatch(r"COMPAT_([A-Z0-9_]+)_BASE_URL", var)
        if m and _env(var):
            name, key = m.group(1).lower(), _env(f"COMPAT_{m.group(1)}_API_KEY")
            srcs.append(Source(f"compat:{name}", f"OpenAI-compatible '{name}'", _env(var).rstrip("/") + "/models",
                               _compatible, {"Authorization": f"Bearer {key}"} if key else {}))
    return srcs, missing


async def _fetch(client: httpx.AsyncClient, src: Source) -> ProviderModels:
    try:
        r = await client.get(src.url, headers=src.headers, params=src.params)
    except httpx.TimeoutException:
        return ProviderModels(src.prefix, src.label, "error", error="timed out")
    except httpx.HTTPError:
        if src.local:
            return ProviderModels(src.prefix, src.label, "not_configured",
                                  needs=f"not running at {src.url.removesuffix('/models')}")
        return ProviderModels(src.prefix, src.label, "error", error=f"can't reach {src.url.split('?')[0]}")
    if r.status_code in (401, 403):
        return ProviderModels(src.prefix, src.label, "error", error=f"key rejected ({r.status_code})")
    if r.status_code >= 400:
        return ProviderModels(src.prefix, src.label, "error", error=f"HTTP {r.status_code}")
    try:
        ids = sorted(set(src.parse(r.json())))
    except (ValueError, KeyError, TypeError):
        return ProviderModels(src.prefix, src.label, "error", error="unexpected response")
    return ProviderModels(src.prefix, src.label, "ok",
                          models=[{"id": f"{src.prefix}:{i}", "name": i} for i in ids])


async def list_catalog(refresh: bool = False) -> dict:
    global _cache, _cache_at
    if _cache is not None and not refresh and time.monotonic() - _cache_at < CACHE_SECONDS:
        return _cache
    srcs, missing = _sources()
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=_transport) as client:
        found = await asyncio.gather(*(_fetch(client, s) for s in srcs))
    order = {"ok": 0, "error": 1, "not_configured": 2}
    providers = sorted([*found, *missing], key=lambda p: (order[p.status], p.label))
    _cache = {"providers": [p.as_dict() for p in providers], "fetched_at": int(time.time())}
    _cache_at = time.monotonic()
    return _cache


def clear_cache() -> None:
    global _cache
    _cache = None
