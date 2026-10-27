"""HTTP-level checks: job endpoints run on the event loop, and capture is all-or-nothing."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.jobs import job_agent
from app.api import deps
from app.vault.store import VaultRegistry


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "_registry", VaultRegistry(tmp_path))
    from app.main import app
    with TestClient(app) as c:
        yield c


def _wait(client, job_id):
    for _ in range(200):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _broken_model():
    def fail(messages, info: AgentInfo) -> ModelResponse:
        raise RuntimeError("provider down")

    async def fail_stream(messages, info: AgentInfo):
        raise RuntimeError("provider down")
        yield {}  # pragma: no cover - makes this an async generator
    return FunctionModel(fail, stream_function=fail_stream)


def test_start_job_endpoint(client):
    r = client.post("/api/vaults/default/jobs/sync")
    assert r.status_code == 202
    job = _wait(client, r.json()["id"])
    assert job["status"] == "succeeded" and "Nothing to sync" in job["result"]["report"]


def test_capture_by_existing_source_is_gone(client):
    assert client.post("/api/vaults/default/jobs/capture", json={}).status_code == 422


def test_failed_capture_saves_nothing_and_can_be_retried(client):
    with job_agent().override(model=_broken_model()):
        r = client.post("/api/vaults/default/capture", data={"text": "# Note\nMARCKS is a PKC substrate."})
        assert r.status_code == 202
        job = _wait(client, r.json()["job"]["id"])
    assert job["status"] == "failed" and "provider down" in job["error"]
    assert job["retryable"] is True
    assert client.get("/api/vaults/default/sources").json() == []
    assert client.get("/api/vaults/default/history").json()[0]["message"] == "init vault"

    with job_agent().override(model=_broken_model()):
        r = client.post(f"/api/jobs/{job['id']}/retry")
        assert r.status_code == 202 and r.json()["params"]["retry_of"] == job["id"]
        _wait(client, r.json()["id"])
    # the payload moved to the new job, so the old one can't be retried twice
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 410


def test_duplicate_capture_is_rejected_without_writing(client):
    from app.api.deps import registry
    v = registry().get("default")
    v.save_source(title="Note", body="MARCKS is a PKC substrate.", source_url="pasted",
                  content_type="note")
    r = client.post("/api/vaults/default/capture", data={"text": "MARCKS is a PKC substrate."})
    assert r.status_code == 409
    assert len(client.get("/api/vaults/default/sources").json()) == 1


def test_capture_fact_check_toggle_is_passed_to_the_job(client):
    assert "verify_captures" in client.get("/api/config").json()
    with job_agent().override(model=_broken_model()):
        off = client.post("/api/vaults/default/capture", data={"text": "a note", "verify": "false"})
        on = client.post("/api/vaults/default/capture", data={"text": "other note", "verify": "true"})
        default = client.post("/api/vaults/default/capture", data={"text": "third note"})
        for r in (off, on, default):
            _wait(client, r.json()["job"]["id"])
    assert off.json()["job"]["params"]["verify"] is False
    assert on.json()["job"]["params"]["verify"] is True
    assert "verify" not in default.json()["job"]["params"]  # falls back to VERIFY_CAPTURES


def test_retry_unknown_job(client):
    assert client.post("/api/jobs/nope/retry").status_code == 404


def test_new_vault_has_no_agent_files(client, tmp_path):
    assert client.post("/api/vaults", json={"name": "fresh"}).status_code == 201
    assert sorted(p.name for p in (tmp_path / "fresh").iterdir()) == [".git", "index.md", "log.md",
                                                                      "sources", "wiki"]
