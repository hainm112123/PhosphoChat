from __future__ import annotations

import pytest
from pydantic_ai import UserError
from pydantic_ai.models.openai import OpenAIChatModel

from app import llm


def test_oxion_model_uses_its_own_key_and_base_url(monkeypatch):
    monkeypatch.setenv("OXION_API_KEY", "cc_test")
    monkeypatch.delenv("OXION_BASE_URL", raising=False)
    model = llm.get_model("oxion:gemma-2-2b")
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "gemma-2-2b"
    assert str(model.client.base_url).rstrip("/") == llm.OXION_DEFAULT_BASE_URL
    assert model.client.api_key == "cc_test"


def test_oxion_without_key_explains(monkeypatch):
    monkeypatch.setenv("OXION_API_KEY", "")
    with pytest.raises(UserError, match="OXION_API_KEY"):
        llm.get_model("oxion:gemma-2-2b")


def test_role_models_fall_back(monkeypatch):
    from app.config import get_settings
    s = get_settings()
    for k, v in {"model": "groq:base", "chat_model": "", "job_model": "", "verify_model": ""}.items():
        monkeypatch.setattr(s, k, v)
    assert llm.role_models() == {"chat": "groq:base", "job": "groq:base", "verify": "groq:base"}

    monkeypatch.setattr(s, "job_model", "groq:big")
    assert llm.role_models() == {"chat": "groq:base", "job": "groq:big", "verify": "groq:big"}

    monkeypatch.setattr(s, "chat_model", "groq:small")
    monkeypatch.setattr(s, "verify_model", "groq:other")
    assert llm.role_models() == {"chat": "groq:small", "job": "groq:big", "verify": "groq:other"}
    assert llm.model_name("anthropic:x", "job") == "anthropic:x"  # explicit override wins


def test_other_providers_pass_through():
    assert llm.get_model("anthropic:claude-sonnet-5-5") == "anthropic:claude-sonnet-5-5"


# --------------------------------------------------------------------------- every provider

LOCAL_VARS = ["OLLAMA_BASE_URL", "OLLAMA_API_KEY", "LMSTUDIO_BASE_URL", "VLLM_BASE_URL",
              "COMPAT_LOCAL_BASE_URL", "COMPAT_LOCAL_API_KEY"]


@pytest.fixture
def clean_env(monkeypatch):
    for v in LOCAL_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(llm, "in_docker", lambda: False)
    llm._build.cache_clear()
    return monkeypatch


def _base(model) -> str:
    return str(model.client.base_url).rstrip("/")


@pytest.mark.parametrize("name, expected", [
    ("claude:claude-sonnet-5-5", "anthropic:claude-sonnet-5-5"),
    ("gemini:gemini-2.5-flash", "google:gemini-2.5-flash"),
    ("google-gla:gemini-2.5-flash", "google:gemini-2.5-flash"),     # old docs keep working
    ("google-vertex:gemini-2.5-flash", "google-cloud:gemini-2.5-flash"),
    ("openai:gpt-5.2", "openai:gpt-5.2"),
    ("openai-chat:gpt-5.2", "openai-chat:gpt-5.2"),
    ("openrouter:anthropic/claude-sonnet-5-5", "openrouter:anthropic/claude-sonnet-5-5"),
    ("mistral:mistral-large-latest", "mistral:mistral-large-latest"),  # pass-through
])
def test_cloud_prefixes_and_aliases(name, expected):
    assert llm.get_model(name) == expected


def test_cloud_strings_build_the_right_models(monkeypatch):
    from pydantic_ai.models import infer_model
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")
    assert type(infer_model(llm.get_model("claude:claude-sonnet-5-5"))).__name__ == "AnthropicModel"
    assert type(infer_model(llm.get_model("gemini:gemini-2.5-flash"))).__name__ == "GoogleModel"
    chat = infer_model(llm.get_model("openai-chat:gpt-5.2"))
    assert type(chat).__name__ == "OpenAIChatModel" and _base(chat) == "https://gateway.example/v1"
    assert type(infer_model(llm.get_model("openai:gpt-5.2"))).__name__ == "OpenAIResponsesModel"


def test_ollama_defaults_and_overrides(clean_env):
    m = llm.get_model("ollama:qwen3:14b")
    assert type(m).__name__ == "OllamaModel" and m.model_name == "qwen3:14b"
    assert _base(m) == "http://localhost:11434/v1"
    clean_env.setenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")
    assert _base(llm.get_model("ollama:qwen3:14b")) == "http://ollama:11434/v1"


def test_local_defaults_inside_docker_reach_the_host(clean_env):
    clean_env.setattr(llm, "in_docker", lambda: True)
    assert _base(llm.get_model("ollama:qwen3:14b")) == "http://host.docker.internal:11434/v1"
    assert _base(llm.get_model("lmstudio:qwen2.5-7b")) == "http://host.docker.internal:1234/v1"


def test_lmstudio_vllm_and_compat(clean_env):
    lm = llm.get_model("lmstudio:qwen2.5-7b-instruct")
    assert isinstance(lm, OpenAIChatModel) and _base(lm) == "http://localhost:1234/v1"
    clean_env.setenv("VLLM_BASE_URL", "http://gpu-box:8000/v1")
    vl = llm.get_model("vllm:Qwen/Qwen3-14B")
    assert isinstance(vl, OpenAIChatModel) and _base(vl) == "http://gpu-box:8000/v1"

    clean_env.setenv("COMPAT_LOCAL_BASE_URL", "http://jan:1337/v1")
    clean_env.setenv("COMPAT_LOCAL_API_KEY", "secret-1")
    c = llm.get_model("compat:local:llama3.1:8b")
    assert isinstance(c, OpenAIChatModel) and c.model_name == "llama3.1:8b"
    assert _base(c) == "http://jan:1337/v1" and c.client.api_key == "secret-1"


def test_compat_errors_name_the_missing_setting(clean_env):
    clean_env.setenv("COMPAT_LOCAL_BASE_URL", "http://jan:1337/v1")
    with pytest.raises(UserError, match="COMPAT_ROUTER_BASE_URL.*Configured endpoints: local"):
        llm.get_model("compat:router:some-model")
    with pytest.raises(UserError, match="compat:<endpoint>:<model-id>"):
        llm.get_model("compat:only-a-model")


def test_check_model_reports_without_leaking(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    clean_env.delenv("GOOGLE_API_KEY", raising=False)
    clean_env.delenv("GEMINI_API_KEY", raising=False)
    ok = llm.check_model("claude:claude-sonnet-5-5")
    assert ok["status"] == "ok" and "very-secret" not in str(ok)
    missing = llm.check_model("gemini:gemini-2.5-flash")
    assert missing["status"] == "missing" and "GOOGLE_API_KEY" in missing["detail"]
    assert llm.check_model("ollama:qwen3:14b")["status"] == "local"
    assert llm.check_model("compat:nowhere:x")["status"] == "missing"
    assert llm.check_model("mistral:x")["status"] == "passthrough"
    assert llm.check_model("no-prefix")["status"] == "invalid"


def test_connection_hint_for_unreachable_local_server():
    import httpx
    req = httpx.Request("POST", "http://host.docker.internal:11434/v1/chat/completions")
    try:
        try:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=req)
        except httpx.ConnectError as inner:
            raise RuntimeError("model request failed") from inner
    except RuntimeError as e:
        hint = llm.connection_hint(e)
    assert hint and "host.docker.internal:11434" in hint and "OLLAMA_HOST=0.0.0.0" in hint
    assert llm.connection_hint(ValueError("unrelated")) is None
