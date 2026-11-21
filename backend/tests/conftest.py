from __future__ import annotations

import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

import pytest  # noqa: E402

from app.vault.store import VaultRegistry  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    return VaultRegistry(tmp_path).create("test")


@pytest.fixture(autouse=True)
def auth_db(tmp_path, monkeypatch):
    """Each test gets its own empty user/session database (never the real data/ one), and the
    .env bootstrap admin is ignored so tests start with no users."""
    from app.auth import deps
    from app.auth.store import AuthStore
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "admin_username", "")
    monkeypatch.setattr(get_settings(), "admin_password", "")
    store = AuthStore(tmp_path / "auth.db")
    deps.set_store(store)
    deps.throttle._fails.clear()
    yield store
    deps.set_store(None)
    store.close()


@pytest.fixture
def make_client(tmp_path, monkeypatch, auth_db):
    """make_client(role) -> a TestClient logged in as a fresh user with that role (or anonymous)."""
    from fastapi.testclient import TestClient

    from app.api import deps as api_deps
    from app.vault.store import VaultRegistry
    monkeypatch.setattr(api_deps, "_registry", VaultRegistry(tmp_path / "vaults"))
    from app.main import app
    clients = []

    def make(role: str | None = "admin", username: str | None = None):
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        if role:
            name = username or f"{role}{len(clients)}"
            auth_db.create_user(name, "password123", role)
            assert c.post("/api/auth/login", json={"username": name, "password": "password123"}).status_code == 200
        return c
    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def no_verify_by_default(monkeypatch):
    """Job tests script the writer only; tests of the fact-check turn it back on."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "verify_captures", False)


@pytest.fixture(autouse=True)
def no_topic_guard_by_default(monkeypatch):
    """Other tests chat and capture filler text ("hi", "first"); test_topic.py turns the
    topic gate back on with a scripted guard model, so nothing reaches a real provider."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "topic_guard", False)


@pytest.fixture(scope="session")
def neo4j_ready():
    from app.graph import signor
    try:
        signor.graph_overview()
    except RuntimeError as e:
        pytest.skip(f"Neo4j not available: {e}")


@pytest.fixture(autouse=True)
def fresh_reading_cache(monkeypatch):
    """Readings are cached per source text and model; tests reuse the same text. The disk
    cache (READING_CACHE_DIR, set by the evaluation) is off unless a test turns it on."""
    from app.agents import reader
    monkeypatch.delenv("READING_CACHE_DIR", raising=False)
    reader._cache.clear()
    yield
    reader._cache.clear()


def reader_model(findings=(), relations=()):
    """A scripted reader: every part gets these findings and relations (dicts)."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    def fn(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                 {"findings": list(findings), "relations": list(relations)})])
    return FunctionModel(fn)


@pytest.fixture(autouse=True)
def offline_reader():
    """Capture and sync always read their sources through the reader agent first: by default
    it finds nothing, so no test reaches a real provider. Tests override it to script notes."""
    from app.agents.reader import reader_agent
    with reader_agent().override(model=reader_model()):
        yield
