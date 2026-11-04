"""Model selection: live catalog parsing, saved choices beat .env, admin-only API."""

from __future__ import annotations

import httpx
import pytest

from app import llm, model_catalog

PROVIDER_VARS = ["GROQ_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY",
                 "GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OXION_API_KEY",
                 "OLLAMA_BASE_URL", "OLLAMA_API_KEY", "LMSTUDIO_BASE_URL", "VLLM_BASE_URL"]

RESPONSES = {
    "api.groq.com": {"data": [{"id": "openai/gpt-oss-120b", "active": True},
                              {"id": "whisper-large-v3", "active": True},
                              {"id": "meta-llama/llama-prompt-guard-2-22m", "active": True},
                              {"id": "old-model", "active": False}]},
    "api.openai.com": {"data": [{"id": "gpt-5.2"}, {"id": "o4-mini"}, {"id": "text-embedding-3-large"},
                                {"id": "gpt-image-1"}, {"id": "whisper-1"}, {"id": "davinci-002"}]},
    "api.anthropic.com": {"data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-haiku-5-5"}]},
    "generativelanguage.googleapis.com": {"models": [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/imagen-3", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/lyria-3.5", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-robotics-er-2-preview", "supportedGenerationMethods": ["generateContent"]}]},
    "openrouter.ai": {"data": [{"id": "anthropic/claude-sonnet-5-5", "supported_parameters": ["tools"]},
                               {"id": "some/no-tools", "supported_parameters": ["temperature"]}]},
    "localhost:11434": {"data": [{"id": "qwen3:14b"}, {"id": "nomic-embed-text"}]},
}


@pytest.fixture
def providers(monkeypatch):
    """No real keys or servers: only what each test sets, answered by a mock transport."""
    for v in PROVIDER_VARS:
        monkeypatch.delenv(v, raising=False)
    for v in [k for k in __import__("os").environ if k.startswith("COMPAT_")]:
        monkeypatch.delenv(v)
    monkeypatch.setattr(llm, "in_docker", lambda: False)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        host = request.url.netloc.decode()
        if "localhost:1234" in host or "localhost:8000" in host:
            raise httpx.ConnectError("refused", request=request)
        if request.headers.get("authorization") == "Bearer bad":
            return httpx.Response(401, json={"error": "invalid key"})
        return httpx.Response(200, json=RESPONSES.get(host, {"data": []}))

    monkeypatch.setattr(model_catalog, "_transport", httpx.MockTransport(handler))
    model_catalog.clear_cache()
    yield monkeypatch, calls
    model_catalog.clear_cache()


def _by_prefix(catalog):
    return {p["prefix"]: p for p in catalog["providers"]}


async def test_catalog_filters_to_chat_models(providers):
    mp, _ = providers
    for k in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
        mp.setenv(k, "good")
    cat = _by_prefix(await model_catalog.list_catalog(refresh=True))
    ids = lambda p: [m["id"] for m in cat[p]["models"]]  # noqa: E731
    assert ids("groq") == ["groq:openai/gpt-oss-120b"]
    assert ids("openai") == ["openai:gpt-5.2", "openai:o4-mini"]
    assert ids("anthropic") == ["anthropic:claude-haiku-5-5", "anthropic:claude-sonnet-5-5"]
    assert ids("google") == ["google:gemini-2.5-flash"]
    assert ids("openrouter") == ["openrouter:anthropic/claude-sonnet-5-5"]
    assert ids("ollama") == ["ollama:qwen3:14b"]               # local, no key needed
    assert cat["lmstudio"]["status"] == "not_configured" and "not running" in cat["lmstudio"]["needs"]
    assert cat["vllm"] == {"prefix": "vllm", "label": "vLLM / llama.cpp server", "status": "not_configured",
                           "models": [], "needs": "VLLM_BASE_URL"}  # never probes port 8000 by default


async def test_catalog_reports_missing_keys_and_rejections(providers):
    mp, _ = providers
    mp.setenv("GROQ_API_KEY", "bad")
    cat = _by_prefix(await model_catalog.list_catalog(refresh=True))
    assert cat["groq"]["status"] == "error" and "key rejected" in cat["groq"]["error"]
    assert cat["anthropic"] == {"prefix": "anthropic", "label": "Claude (Anthropic)",
                                "status": "not_configured", "models": [], "needs": "ANTHROPIC_API_KEY"}


async def test_catalog_lists_compat_endpoints_and_caches(providers):
    mp, calls = providers
    mp.setenv("COMPAT_LOCAL_BASE_URL", "http://localhost:11434/v1")
    cat = _by_prefix(await model_catalog.list_catalog(refresh=True))
    assert [m["id"] for m in cat["compat:local"]["models"]] == ["compat:local:qwen3:14b"]
    n = len(calls)
    await model_catalog.list_catalog()
    assert len(calls) == n                                     # served from the cache


# --------------------------------------------------------------------------- resolution order

def test_saved_choice_beats_env_and_reset_falls_back(auth_db, monkeypatch):
    from app.config import get_settings
    s = get_settings()
    for k, v in {"model": "groq:base", "chat_model": "groq:env-chat", "job_model": "", "verify_model": ""}.items():
        monkeypatch.setattr(s, k, v)
    assert llm.role_models()["chat"] == "groq:env-chat" and llm.role_sources()["chat"] == "env"
    auth_db.set_setting("model.chat", "ollama:qwen3:14b")
    auth_db.set_setting("model.job", "anthropic:claude-sonnet-5-5")
    assert llm.role_models() == {"chat": "ollama:qwen3:14b", "job": "anthropic:claude-sonnet-5-5",
                                 "verify": "anthropic:claude-sonnet-5-5"}
    assert llm.role_sources() == {"chat": "app", "job": "app", "verify": "app (job model)"}
    assert llm.model_name("groq:override", "chat") == "groq:override"   # explicit override wins
    auth_db.delete_setting("model.chat")
    assert llm.role_models()["chat"] == "groq:env-chat"


# --------------------------------------------------------------------------- API

def test_model_api_is_admin_only(make_client):
    user = make_client("user")
    for method, path in [("GET", "/api/models/catalog"), ("GET", "/api/models/settings"),
                         ("PUT", "/api/models/settings"), ("POST", "/api/models/test")]:
        assert user.request(method, path, json={}).status_code == 403, path


def test_save_reset_and_validation(make_client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    admin = make_client("admin")
    r = admin.put("/api/models/settings", json={"chat": "claude:claude-haiku-5-5"})
    assert r.status_code == 200 and r.json()["roles"]["chat"]["source"] == "app"
    assert admin.get("/api/config").json()["models"]["chat"] == "claude:claude-haiku-5-5"
    bad = admin.put("/api/models/settings", json={"job": "gemini:gemini-2.5-flash"})
    assert bad.status_code == 400 and "GOOGLE_API_KEY" in bad.json()["detail"]
    assert admin.put("/api/models/settings", json={"chat": "nonsense"}).status_code == 400
    r = admin.put("/api/models/settings", json={"chat": ""})
    assert r.json()["roles"]["chat"]["source"] != "app"


def test_model_test_endpoint(make_client, monkeypatch):
    from pydantic_ai.models.test import TestModel
    import app.api.models as models_api
    monkeypatch.setattr(models_api, "get_model", lambda name: TestModel(custom_output_text="OK"))
    admin = make_client("admin")
    r = admin.post("/api/models/test", json={"model": "groq:anything"}).json()
    assert r["ok"] is True and r["reply"] == "OK"

    def boom(name):
        raise llm.UserError("Set the `GROQ_API_KEY` environment variable to use Groq.")
    monkeypatch.setattr(models_api, "get_model", boom)
    r = admin.post("/api/models/test", json={"model": "groq:anything"}).json()
    assert r["ok"] is False and "GROQ_API_KEY" in r["error"]
