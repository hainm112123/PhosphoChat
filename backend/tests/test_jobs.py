"""Job pipeline with a scripted model (no API key needed): tools, log, index, commit, rollback."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.agents.jobs import job_agent, run_job
from app.agents.runtime import JobFailed
from app.vault.store import StagedSource, slugify


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


def _stage(title="MARCKS note", body="MARCKS is a PKC substrate.", original=None):
    """A staged source plus the filename the capture job will save it under."""
    staged = StagedSource(title=title, body=body, source_url="pasted", content_type="note",
                          original=original)
    return staged, f"{date.today().isoformat()}-{slugify(title)}.md"


def _final(**report):
    report.setdefault("report", "ok")
    return lambda m, info: [(info.output_tools[0].name, report)]


def _snapshot(vault):
    """Everything a user could see in the vault, to compare before/after a failed job."""
    files = {str(p.relative_to(vault.root)): p.read_bytes()
             for p in vault.root.rglob("*") if p.is_file() and ".git" not in p.parts}
    return files, vault.head()


async def _collect(vault, kind, params, staged=None):
    events = []

    async def emit(ev):
        events.append(ev)
    result = await run_job(vault, kind, params, emit, staged=staged)
    return result, events


# --------------------------------------------------------------------------- capture: success

async def test_capture_job_end_to_end(vault):
    staged, src = _stage()
    model = scripted([
        [("read_source", {"name": src})],
        [("write_page", {"name": "marcks", "content": _page(src)})],
        _final(report="Created [[marcks]].", key_takeaway="x"),
    ])
    params = {}
    with job_agent().override(model=model):
        result, events = await _collect(vault, "capture", params, staged)

    assert params["source"] == src
    assert result["pages_created"] == ["marcks"]
    assert "marcks" in vault.index_entries()           # safety net indexed the new page
    entry = vault.log_entries()[0]
    assert entry.kind == "capture" and f"sources/{src}" in entry.body
    assert "[[marcks]]" in entry.body and "Key takeaway: x" in entry.body
    assert "Contradictions: none" in entry.body and "Fact-check" not in entry.body
    # one commit holds the source and the pages
    assert [h["message"] for h in vault.history()] == [f"capture: {src}", "init vault"]
    assert vault._git("status", "--porcelain").stdout == ""
    assert [e["name"] for e in events if e["type"] == "tool_call"] == ["read_source", "write_page"]
    assert vault.unprocessed_sources() == []


async def test_bad_tool_call_is_retried_not_fatal(vault):
    staged, src = _stage()
    model = scripted([
        [("write_page", {"name": "Bad Name", "content": "no frontmatter"})],
        [("write_page", {"name": "marcks", "content": _page(src)})],
        _final(),
    ])
    with job_agent().override(model=model):
        result, events = await _collect(vault, "capture", {}, staged)
    retries = [e for e in events if e["type"] == "tool_result" and e["retry"]]
    assert retries and "Invalid page name" in retries[0]["content"]
    assert result["pages_created"] == ["marcks"]


async def test_uncited_page_is_rejected_then_fixed(vault):
    staged, src = _stage()
    model = scripted([
        [("write_page", {"name": "marcks", "content": _page(src) + "\nKinases use ATP.\n"})],
        [("write_page", {"name": "marcks", "content": _page(src)})],
        _final(),
    ])
    with job_agent().override(model=model):
        result, events = await _collect(vault, "capture", {}, staged)
    retries = [e for e in events if e["type"] == "tool_result" and e["retry"]]
    assert retries and "Kinases use ATP." in retries[0]["content"]
    assert result["pages_created"] == ["marcks"]


# --------------------------------------------------------------------------- capture: all or nothing

async def test_failed_capture_leaves_vault_untouched(vault):
    staged, src = _stage()
    before = _snapshot(vault)

    def boom(m, info):
        raise RuntimeError("model exploded")
    model = scripted([[("write_page", {"name": "marcks", "content": _page(src)})], boom])
    with job_agent().override(model=model), pytest.raises(RuntimeError):
        await _collect(vault, "capture", {}, staged)
    assert _snapshot(vault) == before                   # no source, no page, same HEAD
    assert vault.list_sources() == [] and vault.page_names() == [] and vault.log_entries() == []


async def test_failed_upload_removes_the_original_file_too(vault):
    staged, src = _stage(title="table", body="profile", original=("table.tsv", b"a\tb\n1\t2\n"))
    before = _snapshot(vault)

    def boom(m, info):
        raise RuntimeError("model exploded")
    with job_agent().override(model=scripted([boom])), pytest.raises(RuntimeError):
        await _collect(vault, "capture", {}, staged)
    assert _snapshot(vault) == before
    assert not list(vault.sources_dir.glob("*.tsv"))


async def test_capture_that_writes_no_pages_fails(vault):
    staged, _ = _stage()
    before = _snapshot(vault)
    with job_agent().override(model=scripted([_final(report="Off-topic source.")])), \
            pytest.raises(JobFailed, match="wrote no wiki pages"):
        await _collect(vault, "capture", {}, staged)
    assert _snapshot(vault) == before


async def test_fact_check_error_fails_the_capture(vault, monkeypatch):
    from app.agents.verify import verifier_agent
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "verify_captures", True)
    staged, src = _stage()
    before = _snapshot(vault)

    def broken_verifier(messages, info):
        raise RuntimeError("verifier down")
    writer = scripted([[("write_page", {"name": "marcks", "content": _page(src)})], _final()])
    with job_agent().override(model=writer), \
            verifier_agent().override(model=FunctionModel(broken_verifier)), \
            pytest.raises(JobFailed, match="fact-check failed"):
        await _collect(vault, "capture", {}, staged)
    assert _snapshot(vault) == before


async def test_capture_without_staged_source_fails_cleanly(vault):
    with pytest.raises(JobFailed, match="Capture it again"):
        await _collect(vault, "capture", {})


async def test_failed_job_keeps_earlier_external_changes(vault):
    """Rollback undoes only the job: files the user added before it are committed and kept."""
    (vault.sources_dir / "dropped.md").write_text("notes", encoding="utf-8")
    staged, _ = _stage()

    def boom(m, info):
        raise RuntimeError("model exploded")
    with job_agent().override(model=scripted([boom])), pytest.raises(RuntimeError):
        await _collect(vault, "capture", {}, staged)
    assert (vault.sources_dir / "dropped.md").read_text(encoding="utf-8") == "notes"
    assert [s["name"] for s in vault.list_sources()] == ["dropped.md"]


# --------------------------------------------------------------------------- other jobs

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
    with job_agent().override(model=scripted([first, _final(report="done", log_lines=["Issues found: 1"])])):
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


# --------------------------------------------------------------------------- fact-check

def _verifier(issues_per_call):
    """Scripted fact-checker: returns the next list of issues on each call."""
    calls = iter(issues_per_call)

    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"issues": next(calls)})])
    return FunctionModel(fn)


async def test_fact_check_sends_unsupported_claims_back(vault, monkeypatch):
    from app.agents.verify import verifier_agent
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "verify_captures", True)
    staged, src = _stage()
    bad = _page(src).replace("MARCKS is phosphorylated by PKC", "PKC uses ATP to phosphorylate MARCKS")
    seen = {}

    def revision(messages, info):
        seen["prompt"] = messages[-1].parts[-1].content
        return [("write_page", {"name": "marcks", "content": _page(src)})]
    final = _final(key_takeaway="x")
    writer = scripted([[("write_page", {"name": "marcks", "content": bad})], final, revision, final])
    issue = {"page": "marcks", "claim": "PKC uses ATP to phosphorylate MARCKS",
             "problem": "not_in_source", "note": "the source does not mention ATP"}
    with job_agent().override(model=writer), verifier_agent().override(model=_verifier([[issue]])):
        result, events = await _collect(vault, "capture", {}, staged)

    assert result["fact_check"]["issues_found"] == 1
    assert "ATP" in seen["prompt"] and "remove the claim" in seen["prompt"]
    assert "ATP" not in vault.read_page("marcks")             # the revision was applied
    assert any(e["type"] == "fact_check" for e in events)
    assert "Fact-check: 1 unsupported claim(s) found" in vault.log_entries()[0].body


async def test_fact_check_clean_pages_need_no_revision(vault, monkeypatch):
    from app.agents.verify import verifier_agent
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "verify_captures", True)
    staged, src = _stage()
    writer = scripted([[("write_page", {"name": "marcks", "content": _page(src)})], _final()])
    with job_agent().override(model=writer), verifier_agent().override(model=_verifier([[]])):
        result, _ = await _collect(vault, "capture", {}, staged)
    assert result["fact_check"] == {"checked_pages": ["marcks"], "issues_found": 0, "issues": []}


async def test_verify_false_skips_fact_check(vault, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "verify_captures", True)
    staged, src = _stage()
    writer = scripted([[("write_page", {"name": "marcks", "content": _page(src)})], _final()])
    with job_agent().override(model=writer):  # no verifier override: it must not be called
        result, _ = await _collect(vault, "capture", {"verify": False}, staged)
    assert result["fact_check"] is None
