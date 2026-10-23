"""HTTP-level checks: endpoints that start jobs must run on the event loop."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.vault.store import VaultRegistry


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "_registry", VaultRegistry(tmp_path))
    from app.main import app
    with TestClient(app) as c:
        yield c


def _wait(client, job_id):
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_start_job_endpoint(client):
    r = client.post("/api/vaults/default/jobs/sync")
    assert r.status_code == 202
    job = _wait(client, r.json()["id"])
    assert job["status"] == "succeeded" and "Nothing to sync" in job["result"]["report"]


def test_capture_retry_needs_existing_source(client):
    r = client.post("/api/vaults/default/jobs/capture", json={"source": "nope.md"})
    assert r.status_code == 400


def test_new_vault_has_no_agent_files(client, tmp_path):
    assert client.post("/api/vaults", json={"name": "fresh"}).status_code == 201
    assert sorted(p.name for p in (tmp_path / "fresh").iterdir()) == [".git", "index.md", "log.md",
                                                                      "sources", "wiki"]
