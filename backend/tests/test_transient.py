"""Busy providers (Gemini 503 "model overloaded", per-minute 429): jobs and chat wait and retry."""

from __future__ import annotations

import json
from datetime import date

from pydantic_ai import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.agents.chat import chat_agent
from app.agents.runtime import is_transient


def busy(status=503, message="This model is currently experiencing high demand."):
    return ModelHTTPError(status_code=status, model_name="gemini-3.5-flash",
                          body={"error": {"code": status, "message": message}})


def test_which_errors_are_transient():
    assert is_transient(busy(503)) and is_transient(busy(500)) and is_transient(busy(429, "per minute"))
    assert not is_transient(busy(429, "Quota exceeded for metric: GenerateRequestsPerDayPerProjectPerModel"))
    assert not is_transient(busy(404, "This model is no longer available"))
    assert not is_transient(busy(413, "Request too large for model"))
    assert not is_transient(ValueError("boom"))
    from pydantic_ai import ModelAPIError
    assert is_transient(ModelAPIError(model_name="m", message="ReadTimeout"))
    assert not is_transient(ModelAPIError(model_name="m", message="tool call validation failed"))


async def test_job_step_waits_and_retries_when_the_provider_is_busy(vault, monkeypatch):
    import app.agents.jobs as jobs_mod
    from app.agents.jobs import job_agent, run_job
    from app.vault.store import StagedSource
    monkeypatch.setattr(jobs_mod, "TRANSIENT_WAITS", (0, 0))
    staged = StagedSource(title="Note", body="ATM phosphorylates TDP1.", source_url="pasted", content_type="note")
    src = f"{date.today().isoformat()}-note.md"
    page = ("---\ntitle: Note\nsummary: s\nsources:\n  - " + src + "\n---\n# Note\n\n"
            "**Summary:** ATM phosphorylates TDP1 [[source: " + src + "]].\n")
    state = {"busy": 2}

    def plan(messages, info):
        if state["busy"]:
            state["busy"] -= 1
            raise busy()
        done = sum(isinstance(m, ModelResponse) for m in messages)
        return ([("write_page", {"name": "note", "content": page})] if done == 0
                else [(info.output_tools[0].name, {"report": "ok"})])

    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[ToolCallPart(n, a) for n, a in plan(messages, info)])

    async def stream(messages, info: AgentInfo):
        yield {i: DeltaToolCall(name=n, json_args=json.dumps(a)) for i, (n, a) in enumerate(plan(messages, info))}
    events = []

    async def emit(ev):
        events.append(ev)
    with job_agent().override(model=FunctionModel(fn, stream_function=stream)):
        result = await run_job(vault, "capture", {}, emit, staged=staged)
    assert result["pages_created"] == ["note"]
    assert sum("provider is busy" in (e.get("message") or "") for e in events) == 2


async def test_job_step_gives_up_after_the_waits(vault, monkeypatch):
    import pytest
    import app.agents.jobs as jobs_mod
    from app.agents.jobs import job_agent, run_job
    from app.vault.store import StagedSource
    monkeypatch.setattr(jobs_mod, "TRANSIENT_WAITS", (0,))

    def always_busy(messages, info):
        raise busy()

    async def stream(messages, info):
        raise busy()
        yield {}  # pragma: no cover
    staged = StagedSource(title="Note", body="ATM phosphorylates TDP1.", source_url="pasted", content_type="note")

    async def emit(ev):
        pass
    with job_agent().override(model=FunctionModel(always_busy, stream_function=stream)), \
            pytest.raises(ModelHTTPError):
        await run_job(vault, "capture", {}, emit, staged=staged)
    assert vault.list_sources() == []  # rolled back


def test_chat_retries_once_when_the_provider_is_busy(make_client, monkeypatch):
    import app.api.chat as chat_api
    from app.config import get_settings
    monkeypatch.setattr(chat_api, "BUSY_WAIT_SECONDS", 0)
    monkeypatch.setattr(get_settings(), "chat_prefetch", False)
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            raise busy()
        return ModelResponse(parts=[TextPart("answer")])

    async def stream(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            raise busy()
        yield "answer"
    c = make_client("user")
    with chat_agent().override(model=FunctionModel(fn, stream_function=stream)):
        with c.stream("POST", "/api/vaults/default/chat", json={"message": "What about ATM?"}) as r:
            raw = r.read().decode()
    assert "provider is busy" in raw and "event: error" not in raw and calls["n"] == 2
