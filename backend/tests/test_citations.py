"""Citations in chat answers are checked by code; slips get a note in the answer."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from app.agents.chat import chat_agent
from app.config import get_settings
from app.graph import signor
from app.knowledge.citations import check_citations, unverified_note
from tests.test_chat_history import _chat

KNOWN = {"SIGNOR-81114"}
PMIDS = {"10558990"}


@pytest.fixture
def fake_graph(monkeypatch):
    def run(query, **params):
        if "signor_ids" in query:
            return [{"s": s} for s in params["ids"] if s in KNOWN]
        return [{"p": p} for p in params["ids"] if p in PMIDS]
    monkeypatch.setattr(signor, "_run", run)


def _page(src):
    return (f"---\ntitle: BAD\nsummary: s\nsources:\n  - {src}\n---\n# BAD\n\n"
            f"**Summary:** BAD is phosphorylated [[source: {src}]].\n")


def test_check_finds_slips_and_accepts_good_citations(vault, fake_graph):
    src = vault.save_source(title="Note", body="RAF1 phosphorylates BAD (PMID 11111111).", source_url="pasted",
                            content_type="note")
    vault.write_page("bad", _page(src))
    answer = (f"RAF1 phosphorylates BAD (SIGNOR-81114; PMID 10558990) [[bad]] [[source: {src}]]. "
              "Also SIGNOR-81115, PMID 99999999, PMID 11111111, [[id: bad]] and [[source: nope.md]].")
    c = check_citations(answer, vault)
    assert c.invalid == ["99999999", "SIGNOR-81115", "nope.md"]  # 11111111 is quoted in a source; [[id: bad]] repaired
    note = unverified_note(c)
    assert "SIGNOR-81115" in note and "Unverified citations" in note
    assert unverified_note(check_citations("Nothing cited.", vault)) is None


def test_graph_down_reports_nothing_wrong(vault, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("Graph database unreachable")
    monkeypatch.setattr(signor, "_run", down)
    c = check_citations("SIGNOR-81115 PMID 99999999", vault)
    assert c.invalid == [] and not c.graph_checked


def test_chat_answer_gets_a_note_for_unverified_citations(make_client, monkeypatch, fake_graph):
    monkeypatch.setattr(get_settings(), "chat_prefetch", False)
    client = make_client("user")

    def fn(messages, info):
        return ModelResponse(parts=[TextPart("RAF1 phosphorylates BAD (SIGNOR-81115).")])

    async def stream(messages, info):
        yield "RAF1 phosphorylates BAD (SIGNOR-81115)."
    with chat_agent().override(model=FunctionModel(fn, stream_function=stream)):
        _, text, events = _chat(client, "Which kinases phosphorylate BAD?")
    assert text.startswith("RAF1 phosphorylates BAD (SIGNOR-81115).") and "Unverified citations" in text
    assert "done" in events


def test_link_slips_seen_in_real_answers_are_repaired():
    from app.knowledge.citations import repair_links
    cases = {
        "[[[[cold-ischemia|cold-ischemia]]]]": "[[cold-ischemia|cold-ischemia]]",
        "[[source: [[kinase-activity-inference]]]]": "[[kinase-activity-inference]]",
        "[[source: [[cold-ischemia]] | 2026-11-20-1495.md]]": "[[cold-ischemia]] [[source: 2026-11-20-1495.md]]",
        "[[id: kinase-activity-inference]]": "[[kinase-activity-inference]]",
        "[[page-name: kinase-activity-inference]]": "[[kinase-activity-inference]]",
        "[[-kinase-activity-inference]]": "[[kinase-activity-inference]]",
        "[[synth: kinase-activity-inference]]": "[[kinase-activity-inference]]",
        "[[kinase-activity-inference, source: 2026-11-20-s41467-025-59779-y.md]]":
            "[[kinase-activity-inference]] [[source: 2026-11-20-s41467-025-59779-y.md]]",
        "[[source: a.md]] and [[bad]]": "[[source: a.md]] and [[bad]]",   # correct links are left alone
    }
    for slip, fixed in cases.items():
        assert repair_links(slip)[0] == fixed, slip
    assert repair_links("[[source: a.md]] and [[bad]]")[1] == 0


def test_repaired_links_are_not_reported_as_unverified(vault, fake_graph):
    src = vault.save_source(title="Note", body="x", source_url="pasted", content_type="note")
    vault.write_page("bad", _page(src))
    c = check_citations("BAD [[source: [[bad]]]] and [[id: bad]].", vault)
    assert c.invalid == [] and c.format_slips == 2
