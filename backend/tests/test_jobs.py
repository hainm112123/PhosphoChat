"""Job pipeline with a scripted model (no API key needed): tools, log, index, commit, rollback."""

from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.agents.jobs import job_agent, run_job


def scripted(steps):
    """A (streaming) model that plays back one list of tool calls per turn."""
    def calls_for(messages, info: AgentInfo):
        turn = sum(1 for m in messages if isinstance(m, ModelResponse))
        return steps[turn](messages, info) if callable(steps[turn]) else steps[turn]

    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[ToolCallPart(n, a) for n, a in calls_for(messages, info)])

    async def stream_fn(messages, info: AgentInfo):
        yield {i: DeltaToolCall(name=n, json_args=json.dumps(a))
               for i, (n, a) in enumerate(calls_for(messages, info))}
    return FunctionModel(fn, stream_function=stream_fn)


def _page(src):
    return ("---\ntitle: MARCKS\nsummary: A PKC substrate.\nsources:\n  - " + src + "\n---\n"
            "# MARCKS\n\n**Summary:** MARCKS is phosphorylated by PKC [[source: " + src + "]].\n")


async def _collect(vault, kind, params):
    events = []

    async def emit(ev):
        events.append(ev)
    result = await run_job(vault, kind, params, emit)
    return result, events


async def test_capture_job_end_to_end(vault):
    src = vault.save_source(title="MARCKS note", body="MARCKS is a PKC substrate.",
                            source_url="pasted", content_type="note")
    final = lambda m, info: [(info.output_tools[0].name,  # noqa: E731
                              {"report": "Created [[marcks]].", "log_lines": ["Key takeaway: x"]})]
    model = scripted([
        [("read_source", {"name": src})],
        [("write_page", {"name": "marcks", "content": _page(src)})],
        final,
    ])
    with job_agent().override(model=model):
        result, events = await _collect(vault, "capture", {"source": src})

    assert result["pages_created"] == ["marcks"]
    assert "marcks" in vault.index_entries()           # safety net indexed the new page
    entry = vault.log_entries()[0]
    assert entry.kind == "capture" and f"sources/{src}" in entry.body
    assert "[[marcks]]" in entry.body and "Key takeaway: x" in entry.body
    msgs = [h["message"] for h in vault.history()]
    assert msgs[:2] == [f"capture: {src}", f"source: {src}"]
    assert [e["name"] for e in events if e["type"] == "tool_call"] == ["read_source", "write_page"]
    assert vault.unprocessed_sources() == []


async def test_bad_tool_call_is_retried_not_fatal(vault):
    src = vault.save_source(title="n", body="b", source_url="pasted", content_type="note")
    final = lambda m, info: [(info.output_tools[0].name, {"report": "ok"})]  # noqa: E731
    model = scripted([
        [("write_page", {"name": "Bad Name", "content": "no frontmatter"})],
        [("write_page", {"name": "marcks", "content": _page(src)})],
        final,
    ])
    with job_agent().override(model=model):
        result, events = await _collect(vault, "capture", {"source": src})
    retries = [e for e in events if e["type"] == "tool_result" and e["retry"]]
    assert retries and "Invalid page name" in retries[0]["content"]
    assert result["pages_created"] == ["marcks"]


async def test_failed_job_rolls_back(vault):
    src = vault.save_source(title="n", body="b", source_url="pasted", content_type="note")

    def boom(m, info):
        raise RuntimeError("model exploded")
    model = scripted([[("write_page", {"name": "marcks", "content": _page(src)})], boom])
    with job_agent().override(model=model), pytest.raises(RuntimeError):
        await _collect(vault, "capture", {"source": src})
    assert vault.page_names() == []
    assert vault.log_entries() == []
    assert (vault.sources_dir / src).is_file()          # the source itself is kept (committed)


async def test_sync_with_nothing_to_do(vault):
    result, events = await _collect(vault, "sync", {})
    assert "Nothing to sync" in result["report"]


async def test_lint_prestep_autofixes_index(vault):
    src = vault.save_source(title="n", body="b", source_url="pasted", content_type="note")
    vault.write_page("marcks", _page(src))
    seen = {}

    def first(messages, info):
        seen["prompt"] = messages[0].parts[-1].content
        return [("write_page", {"name": "lint-report", "content":
                 "---\ntitle: Lint report\nsummary: 1 issue\nsources: []\n---\n# Lint report\nok\n"})]
    final = lambda m, info: [(info.output_tools[0].name,  # noqa: E731
                              {"report": "done", "log_lines": ["Issues found: 1"]})]
    with job_agent().override(model=scripted([first, final])):
        result, _ = await _collect(vault, "lint", {})
    assert "indexed [[marcks]]" in seen["prompt"]
    assert "marcks" in vault.index_entries() and "lint-report" not in vault.index_entries()
    assert vault.log_entries()[0].kind == "lint"


def test_tool_results_are_capped(monkeypatch):
    from app.config import get_settings
    from app.tools.agent_tools import out
    monkeypatch.setattr(get_settings(), "tool_output_chars", 500)
    text = out({"total": 300, "results": [{"enzyme": f"K{i}", "sites": ["Ser1"]} for i in range(300)]})
    assert len(text) <= 600 and "shortened" in text
