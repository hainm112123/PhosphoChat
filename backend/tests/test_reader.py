"""Code reads every source: sections and parts, verbatim quotes, claims by code, lean writer."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.agents import reader
from app.agents.reader import compact_history, read_in_parts, reader_agent, split_parts
from app.knowledge import claims

PROTEINS = {"ATM": "Q13315", "TDP1": "Q9NUW8", "AKT1": "P31749", "BAD": "Q92934"}


@pytest.fixture
def offline_claims(monkeypatch):
    def resolve(name, role):
        if name.upper() not in PROTEINS:
            raise claims.VaultError(f"{role} '{name}' is not in the graph")
        return {"id": PROTEINS[name.upper()], "name": name.upper()}
    monkeypatch.setattr(claims, "_resolve_one", resolve)
    monkeypatch.setattr(claims, "statuses", lambda cs: [{**c, "status": "new", "detail": ""} for c in cs])


def _long_paper() -> str:
    filler = "Mass spectrometry was performed on all samples with standard settings. " * 60
    return (f"<!-- page 1 -->\nIntroduction.\n\n{filler}\n\n<!-- page 2 -->\nResults.\n\n"
            "We show that ATM phosphorylates TDP1 at S81 after DNA damage.\n\n" + filler
            + "\n\n<!-- page 3 -->\nAKT1 phosphorylates BAD at Ser136 in these cells.\n\n" + filler)


def test_split_parts_keeps_pages_and_size():
    parts = split_parts(_long_paper(), 4000)
    assert len(parts) >= 3 and all(len(p.text) <= 8000 for p in parts)
    assert parts[0].page == 1 and any(p.page >= 2 for p in parts[1:])


SKIP = r"references|(materials and )?methods|acknowledgements"


def _sectioned_paper() -> str:
    para = "ATM phosphorylates TDP1 at S81 after DNA damage in these cells. " * 20
    return ("<!-- page 1 -->\n## Abstract\n\nA short abstract about TDP1.\n\n## Materials and Methods\n\n"
            "Cells were lysed in buffer.\n\n## Protein extraction\n\nProteins were digested.\n\n"
            f"<!-- page 2 -->\n## 2. Results\n\n{para}\n\n<!-- page 3 -->\nMore results on BAD.\n\n"
            "## References\n\n1. Smith J. Nature 2020.\n\n## Acknowledgements\n\nWe thank everyone.")


def test_split_parts_cuts_at_sections_and_skips_methods_and_references():
    parts = split_parts(_sectioned_paper(), 4000, SKIP)
    text = "\n".join(p.text for p in parts)
    assert "Cells were lysed" not in text and "Proteins were digested" not in text  # methods and its sub-section
    assert "Smith J" not in text and "We thank" not in text
    assert "## 2. Results" in text and "More results on BAD" in text
    assert parts[0].where("ATM phosphorylates TDP1 at S81") == "Results, p. 2"
    assert parts[0].where("More results on BAD") == "Results, p. 3"


def test_skipping_is_undone_when_it_would_drop_almost_everything():
    body = "## Methods\n\n" + "ATM phosphorylates TDP1 in our hands. " * 50
    parts = split_parts(body, 4000, SKIP)
    assert parts and "ATM phosphorylates TDP1" in parts[0].text


def test_a_short_source_goes_to_the_writer_whole():
    notes = reader.Notes(source="s.md", parts=1, full_text="ATM phosphorylates TDP1.")
    assert notes.render(5000) == "Full text:\nATM phosphorylates TDP1."


def _notes_model(seen: list):
    def fn(messages, info: AgentInfo):
        text = messages[-1].parts[-1].content
        seen.append(len(text))
        findings, relations = [], []
        if "ATM phosphorylates TDP1" in text:
            findings = [{"statement": "ATM phosphorylates TDP1.", "quote": "ATM phosphorylates TDP1 at S81 after DNA damage"},
                        {"statement": "Invented.", "quote": "a sentence that is not in the paper"}]
            relations = [{"enzyme": "ATM", "target": "TDP1", "site": "S81",
                          "quote": "ATM phosphorylates TDP1 at S81 after DNA damage"},
                         {"enzyme": "NOTAPROTEIN", "target": "TDP1", "quote": "ATM phosphorylates TDP1"}]
        if "AKT1 phosphorylates BAD" in text:
            relations = [{"enzyme": "AKT1", "target": "BAD", "site": "Ser136",
                          "quote": "AKT1 phosphorylates BAD at Ser136 in these cells"}]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                 {"findings": findings, "relations": relations})])
    return FunctionModel(fn)


async def test_read_in_parts_keeps_only_verbatim_quotes_and_records_claims(vault, offline_claims, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "capture_part_chars", 4000)
    name = vault.save_source(title="Long paper", body=_long_paper(), source_url="pasted", content_type="paper")
    seen, events, found = [], [], []

    async def emit(ev):
        events.append(ev)
    with reader_agent().override(model=_notes_model(seen)):
        notes = await read_in_parts(vault, name, emit, found)
    assert len(seen) == notes.parts >= 3 and max(seen) < 9000      # every part read, each request small
    assert [f.quote for _, f in notes.findings] == ["ATM phosphorylates TDP1 at S81 after DNA damage"]
    assert notes.rejected_quotes == 1 and notes.rejected_relations == 1 and notes.claims_added == 2
    assert {(c["enzyme"], c["target"], c["site"]) for c in found} == {("ATM", "TDP1", "Ser81"), ("AKT1", "BAD", "Ser136")}
    assert len(vault.read_relations()) == 2
    assert "(p. 2) ATM phosphorylates TDP1." in notes.render(2000)


def test_compact_history_drops_old_page_text():
    page = "x" * 2000
    msgs = [ModelRequest(parts=[UserPromptPart("capture")]),
            ModelResponse(parts=[ToolCallPart("write_page", {"name": "atm", "content": page})]),
            ModelRequest(parts=[ToolReturnPart("write_page", "ok", tool_call_id="1")]),
            ModelResponse(parts=[ToolCallPart("write_page", {"name": "tdp1", "content": page})])]
    out = compact_history(msgs)
    assert "page written earlier, 2000 characters" in out[1].parts[0].args_as_dict()["content"]
    assert out[3].parts[0].args_as_dict()["content"] == page    # the latest response is left alone


async def test_long_capture_reads_in_parts_then_writes_from_notes(vault, offline_claims, monkeypatch):
    from app.agents.jobs import job_agent, run_job
    from app.config import get_settings
    from app.vault.store import StagedSource
    monkeypatch.setattr(get_settings(), "capture_part_chars", 4000)
    staged = StagedSource(title="Long paper", body=_long_paper(), source_url="pasted", content_type="paper")
    src = f"{date.today().isoformat()}-long-paper.md"
    page = ("---\ntitle: ATM\nsummary: ATM and TDP1.\nsources:\n  - " + src + "\n---\n# ATM\n\n"
            "**Summary:** ATM phosphorylates TDP1 at S81 [[source: " + src + "]].\n")
    prompts = []

    def plan(messages, info):
        prompts.append(messages[0].parts[-1].content)
        done = sum(isinstance(m, ModelResponse) for m in messages)
        return ([("write_page", {"name": "atm", "content": page})] if done == 0
                else [(info.output_tools[0].name, {"report": "ok"})])

    def fn(messages, info):
        return ModelResponse(parts=[ToolCallPart(n, a) for n, a in plan(messages, info)])

    async def stream(messages, info):
        yield {i: DeltaToolCall(name=n, json_args=json.dumps(a)) for i, (n, a) in enumerate(plan(messages, info))}
    events = []

    async def emit(ev):
        events.append(ev)
    with reader_agent().override(model=_notes_model([])), \
            job_agent().override(model=FunctionModel(fn, stream_function=stream)):
        result = await run_job(vault, "capture", {}, emit, staged=staged)
    assert result["pages_created"] == ["atm"] and len(result["claims"]) == 2
    assert "# Reading notes" in prompts[0] and "ATM phosphorylates TDP1 at S81 after DNA damage" in prompts[0]
    assert any("Reading" in (e.get("message") or "") and "part 1 of" in e["message"] for e in events)
    assert "Relations recorded: 2" in vault.read_log_text()


async def test_writer_retries_with_shorter_notes_when_the_request_is_too_large(vault, offline_claims, monkeypatch):
    from pydantic_ai import ModelHTTPError
    from app.agents.jobs import job_agent, run_job
    from app.config import get_settings
    from app.vault.store import StagedSource
    monkeypatch.setattr(get_settings(), "capture_part_chars", 4000)
    staged = StagedSource(title="Long paper", body=_long_paper(), source_url="pasted", content_type="paper")
    src = f"{date.today().isoformat()}-long-paper.md"
    page = ("---\ntitle: ATM\nsummary: ATM and TDP1.\nsources:\n  - " + src + "\n---\n# ATM\n\n"
            "**Summary:** ATM phosphorylates TDP1 at S81 [[source: " + src + "]].\n")
    sizes = []

    def plan(messages, info):
        if not sizes or len(sizes) == 1 and not any(isinstance(m, ModelResponse) for m in messages):
            sizes.append(len(messages[0].parts[-1].content))
            if len(sizes) == 1:
                raise ModelHTTPError(status_code=413, model_name="openai/gpt-oss-120b",
                                     body={"error": {"message": "Request too large for model"}})
        done = sum(isinstance(m, ModelResponse) for m in messages)
        return ([("write_page", {"name": "atm", "content": page})] if done == 0
                else [(info.output_tools[0].name, {"report": "ok"})])

    def fn(messages, info):
        return ModelResponse(parts=[ToolCallPart(n, a) for n, a in plan(messages, info)])

    async def stream(messages, info):
        yield {i: DeltaToolCall(name=n, json_args=json.dumps(a)) for i, (n, a) in enumerate(plan(messages, info))}
    events = []

    async def emit(ev):
        events.append(ev)
    with reader_agent().override(model=_notes_model([])), \
            job_agent().override(model=FunctionModel(fn, stream_function=stream)):
        result = await run_job(vault, "capture", {}, emit, staged=staged)
    assert result["pages_created"] == ["atm"]
    assert any("shorter notes" in (e.get("message") or "") for e in events)
    assert sum("part 1 of" in (e.get("message") or "") for e in events) == 1   # read once


async def test_reading_stops_at_the_first_part_when_the_quota_is_gone(vault, offline_claims, monkeypatch):
    from pydantic_ai import ModelHTTPError
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "capture_part_chars", 4000)
    name = vault.save_source(title="Long paper", body=_long_paper(), source_url="pasted", content_type="paper")
    calls = []

    def fn(messages, info):
        calls.append(1)
        raise ModelHTTPError(status_code=429, model_name="m", body={"error": {"message": "Rate limit reached ... tokens per day (TPD)"}})

    async def emit(ev):
        pass
    with reader_agent().override(model=FunctionModel(fn)), pytest.raises(ModelHTTPError):
        await read_in_parts(vault, name, emit, [])
    assert len(calls) == 1


async def test_a_malformed_reply_is_retried_then_the_part_skipped(vault, offline_claims, monkeypatch):
    from pydantic_ai import ModelHTTPError
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "capture_part_chars", 4000)
    name = vault.save_source(title="Long paper", body=_long_paper(), source_url="pasted", content_type="paper")
    calls = []

    def fn(messages, info):
        calls.append(1)
        if len(calls) <= 2:  # the first part fails twice: retried once, then skipped
            raise ModelHTTPError(status_code=400, model_name="m", body={"error": {"code": "output_parse_failed",
                                 "message": "Parsing failed. The model generated output that could not be parsed."}})
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"findings": [], "relations": []})])

    async def emit(ev):
        pass
    with reader_agent().override(model=FunctionModel(fn)):
        notes = await read_in_parts(vault, name, emit, [])
    assert notes.failed_parts == 1 and len(calls) == notes.parts + 1


def test_compact_history_drops_old_reasoning_and_cuts_old_results():
    from pydantic_ai.messages import TextPart, ThinkingPart
    big = "y" * 5000
    msgs = [ModelRequest(parts=[UserPromptPart("capture")]),
            ModelResponse(parts=[ThinkingPart("long reasoning " * 200), ToolCallPart("read_index", {}, tool_call_id="a")]),
            ModelRequest(parts=[ToolReturnPart("read_index", big, tool_call_id="a")]),
            ModelResponse(parts=[ThinkingPart("more reasoning"), ToolCallPart("read_page", {"name": "x"}, tool_call_id="b")]),
            ModelRequest(parts=[ToolReturnPart("read_page", big, tool_call_id="b")]),
            ModelResponse(parts=[TextPart("ok")])]
    out = compact_history(msgs)
    assert not any(isinstance(p, ThinkingPart) for p in out[1].parts + out[3].parts)
    assert len(out[2].parts[0].content) < 1600 and "cut" in out[2].parts[0].content
    assert out[4].parts[0].content == big   # the latest result stays whole


async def test_a_retried_capture_reuses_the_reading(vault, offline_claims, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "capture_part_chars", 4000)
    reader._cache.clear()
    name = vault.save_source(title="Long paper", body=_long_paper(), source_url="pasted", content_type="paper")
    seen = []

    async def emit(ev):
        pass
    with reader_agent().override(model=_notes_model(seen)):
        first = await read_in_parts(vault, name, emit, [])
        calls = len(seen)
        vault.relations_path.unlink()             # the failed job's rollback removed the claims
        again = await read_in_parts(vault, name, emit, [])
    assert len(seen) == calls                     # no model call the second time
    assert again.claims_added == first.claims_added == 2 and len(vault.read_relations()) == 2


def test_a_long_history_is_cut_under_the_budget():
    big = "z" * 3000
    msgs = [ModelRequest(parts=[UserPromptPart("capture " + "n" * 7000)])]   # the task and notes stay whole
    for i in range(8):
        msgs += [ModelResponse(parts=[ToolCallPart("read_page", {"name": f"p{i}"}, tool_call_id=f"c{i}")]),
                 ModelRequest(parts=[ToolReturnPart("read_page", big, tool_call_id=f"c{i}")])]
    out = compact_history(msgs)
    assert len(out[0].parts[0].content) > 7000
    assert sum(reader._chars(m) for m in out[1:]) <= reader.HISTORY_CHARS
    assert out[-1].parts[0].content == big


async def test_a_part_too_large_for_the_model_is_read_in_halves(vault, offline_claims, monkeypatch):
    from pydantic_ai import ModelHTTPError
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "capture_part_chars", 4000)
    body = "## Results\n\n" + "\n\n".join(f"Paragraph {i}: ATM phosphorylates TDP1 in cells. " * 4 for i in range(40))
    name = vault.save_source(title="Paper", body=body, source_url="pasted", content_type="paper")
    sizes = []

    def fn(messages, info):
        text = messages[-1].parts[-1].content
        sizes.append(len(text))
        if len(text) > 3000:   # a cap the full parts exceed
            raise ModelHTTPError(status_code=413, model_name="m", body={"error": {"message": "Request too large for model"}})
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"findings": [], "relations": []})])

    async def emit(ev):
        pass
    with reader_agent().override(model=FunctionModel(fn)):
        notes = await read_in_parts(vault, name, emit, [])
    assert notes.failed_parts == 0 and notes.parts > len(split_parts(body, 4000)) and max(sizes[-1:]) <= 3000
