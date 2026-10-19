from __future__ import annotations

import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

import pytest  # noqa: E402

from app.vault.store import VaultRegistry  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    return VaultRegistry(tmp_path).create("test")


@pytest.fixture(scope="session")
def neo4j_ready():
    from app.graph import signor
    try:
        signor.graph_overview()
    except RuntimeError as e:
        pytest.skip(f"Neo4j not available: {e}")
