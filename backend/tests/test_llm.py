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
