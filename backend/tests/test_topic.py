"""Topic gate: code check first, a scripted guard model when it can't tell (offline)."""

from __future__ import annotations

import json
import time

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.chat import chat_agent
from app.agents.jobs import job_agent
from app.config import get_settings
from app.knowledge import entities, topic

PROTEINS = {"ATM", "TDP1", "EGFR", "MARCKS"}
RECIPE = ("Classic tomato pasta\n\nBoil the spaghetti in salted water. Fry garlic in olive oil, "
          "add chopped tomatoes and basil, simmer for ten minutes and toss with the pasta.")


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "topic_guard", True)
    monkeypatch.setattr(get_settings(), "chat_prefetch", False)
    # A fake protein index instead of the graph.
    monkeypatch.setattr(topic, "extract_mentions", lambda text, limit=20: [
        entities.Mention(text=w, ids=[w], name=w) for w in sorted(PROTEINS) if w in text])


def guard(calls: list, *, off: tuple[str, ...] = (), fail: bool = False):
    """A guard model that says off-topic when the text contains one of `off`."""
    def fn(messages, info: AgentInfo):
        text = messages[-1].parts[-1].content
        calls.append(text)
        if fail:
            raise RuntimeError("guard model down")
        bad = next((w for w in off if w in text.lower()), None)
        return ModelResponse(parts=[TextPart(f"off-topic: about {bad}" if bad else "on-topic: biomedical")])
    return FunctionModel(fn)


def answering(calls: list):
    def fn(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart("answer")])

    async def stream(messages, info):
        calls.append(1)
        yield "answer"
    return FunctionModel(fn, stream_function=stream)


def _chat(client, message, session=None):
    body = {"message": message, **({"session_id": session} if session else {})}
    with client.stream("POST", "/api/vaults/default/chat", json=body) as r:
        raw = r.read().decode()
    events = {}
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.setdefault(lines["event"], []).append(json.loads(lines["data"]))
    return events


# --------------------------------------------------------------------------- code check

@pytest.mark.parametrize("q", ["Which kinases phosphorylate TDP1?", "Ser81 and S81 sites", "What about ATM?",
                               "hi", "What can you do?", "thanks!"])
def test_code_says_yes(q):
    assert topic.quick_check(q, kind="question").on_topic


@pytest.mark.parametrize("q", ["best protein powder for the gym", "write a poem about the sea",
                               "Which drugs are used for Parkinson's disease?"])
def test_code_leaves_it_to_the_model(q):
    assert topic.quick_check(q, kind="question") is None


def test_follow_ups_count_only_in_an_on_topic_conversation():
    assert topic.quick_check("why?", kind="question", previous="How does ATM regulate TDP1?").on_topic
    assert topic.quick_check("why?", kind="question", previous="write a poem") is None


def test_sources_need_two_signals():
    assert topic.quick_check("ATM phosphorylates TDP1.", kind="source").on_topic
    assert topic.quick_check(RECIPE, kind="source") is None


def test_wiki_page_terms():
    assert topic.has_topic_terms("MARCKS is a PKC substrate.")
    assert not topic.has_topic_terms(RECIPE)


async def test_guard_model_verdicts_and_failures():
    calls = []
    with topic.guard_agent().override(model=guard(calls, off=("poem",))):
        assert not (await topic.check_question("write a poem about the sea")).on_topic
        assert (await topic.check_question("Which drugs treat Parkinson's disease?")).by == "model"
        assert (await topic.check_question("Which kinases phosphorylate TDP1?")).by == "code"
    assert len(calls) == 2  # the code decided the third one
    with topic.guard_agent().override(model=guard([], fail=True)):
        v = await topic.check_question("write a poem")
    assert v.on_topic and v.by == "error"  # a broken guard lets the input through

    def unclear(messages, info):
        return ModelResponse(parts=[TextPart("Sure! Here's a poem...")])
    with topic.guard_agent().override(model=FunctionModel(unclear)):
        assert (await topic.check_question("write a poem")).by == "error"


async def test_gate_off_never_asks(monkeypatch):
    monkeypatch.setattr(get_settings(), "topic_guard", False)
    calls = []
    with topic.guard_agent().override(model=guard(calls, off=("poem",))):
        assert (await topic.check_question("write a poem")).by == "off"
        assert (await topic.check_source("Pasta", RECIPE)).by == "off"
    assert not calls


# --------------------------------------------------------------------------- chat

def test_chat_refuses_off_topic_without_the_chat_model(make_client):
    c, guard_calls, chat_calls = make_client("user"), [], []
    with topic.guard_agent().override(model=guard(guard_calls, off=("poem",))), \
            chat_agent().override(model=answering(chat_calls)):
        ev = _chat(c, "Write me a poem about the sea")
        sid = ev["session"][0]["session_id"]
        assert "only answers questions about kinases" in ev["off_topic"][0]["message"]
        assert "text" not in ev and not chat_calls
        ev = _chat(c, "How does ATM regulate TDP1?", sid)  # on topic by code: no guard call
        assert "".join(e["delta"] for e in ev["text"]) == "answer" and len(guard_calls) == 1
    turns = c.get(f"/api/vaults/default/conversations/{sid}").json()["turns"]
    assert "only answers" in turns[0]["ui"]["refused"] and "refused" not in turns[1]["ui"]


def test_refused_turn_is_not_in_the_history(make_client):
    c, seen = make_client("user"), []

    def fn(messages, info):
        seen.append(sum(1 for m in messages for p in getattr(m, "parts", []) if p.part_kind == "user-prompt"))
        return ModelResponse(parts=[TextPart("answer")])

    async def stream(messages, info):
        yield fn(messages, info).parts[0].content
    with topic.guard_agent().override(model=guard([], off=("poem",))), \
            chat_agent().override(model=FunctionModel(fn, stream_function=stream)):
        sid = _chat(c, "What about ATM?")["session"][0]["session_id"]
        _chat(c, "now write a poem", sid)
        _chat(c, "And TDP1?", sid)
    assert seen == [1, 2]  # ATM, then TDP1 with only the ATM turn before it


def test_chat_lets_the_question_through_when_the_guard_fails(make_client):
    c, chat_calls = make_client("user"), []
    with topic.guard_agent().override(model=guard([], fail=True)), \
            chat_agent().override(model=answering(chat_calls)):
        ev = _chat(c, "write a poem")
    assert "off_topic" not in ev and chat_calls


# --------------------------------------------------------------------------- capture and sync

def _wait(client, job_id):
    for _ in range(200):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _broken():
    def fail(messages, info):
        raise RuntimeError("no job model in this test")

    async def fail_stream(messages, info):
        raise RuntimeError("no job model in this test")
        yield ""  # pragma: no cover
    return FunctionModel(fail, stream_function=fail_stream)


def test_capture_blocks_off_topic_and_admin_can_override(make_client):
    admin = make_client("admin", username="boss")
    with topic.guard_agent().override(model=guard([], off=("spaghetti",))), \
            job_agent().override(model=_broken()):
        before = len(admin.get("/api/vaults/default/jobs").json())
        r = admin.post("/api/vaults/default/capture", data={"text": RECIPE})
        assert r.status_code == 422 and r.json()["detail"].startswith("Off-topic source")
        assert len(admin.get("/api/vaults/default/jobs").json()) == before  # no job started

        r = admin.post("/api/vaults/default/capture", data={"text": RECIPE, "capture_anyway": "true"})
        assert r.status_code == 202
        job = _wait(admin, r.json()["job"]["id"])
        assert any("overridden by boss" in e.get("message", "") for e in job["events"])

        ok = admin.post("/api/vaults/default/capture", data={"text": "ATM phosphorylates TDP1 at Ser81."})
        assert ok.status_code == 202  # on topic by code
        _wait(admin, ok.json()["job"]["id"])


def test_save_to_wiki_is_not_topic_checked(make_client):
    admin, calls = make_client("admin"), []
    with topic.guard_agent().override(model=guard(calls, off=("spaghetti",))), \
            job_agent().override(model=_broken()):
        r = admin.post("/api/vaults/default/chat/save", json={"question": "q", "answer": RECIPE})
        assert r.status_code == 202
        _wait(admin, r.json()["job"]["id"])
    assert not calls


async def test_sync_skips_off_topic_sources(vault):
    from tests.test_jobs import _collect, _final, scripted
    for name, body in [("pasta.md", RECIPE), ("atm.md", "ATM phosphorylates TDP1 at Ser81 [1].")]:
        (vault.sources_dir / name).write_text(body, encoding="utf-8")
    page = ("---\ntitle: ATM\nsummary: ATM and TDP1.\nsources:\n  - atm.md\n---\n# ATM\n\n"
            "**Summary:** ATM phosphorylates TDP1 [[source: atm.md]].\n")
    seen = []

    def write(messages, info):
        seen.append(messages[0].parts[-1].content)
        return [("write_page", {"name": "atm", "content": page})]
    with topic.guard_agent().override(model=guard([], off=("spaghetti",))), \
            job_agent().override(model=scripted([write, _final(report="Synced.")])):
        result, events = await _collect(vault, "sync", {})
    assert "atm.md" in seen[0] and "pasta.md" not in seen[0]
    assert any("pasta.md looks off-topic" in w for w in result["warnings"])
    assert "pasta.md" in vault.unprocessed_sources()  # looked at again next time
    assert "Skipped as off-topic: 1 source(s)" in vault.read_log_text()


async def test_off_topic_page_is_flagged(vault):
    from tests.test_jobs import _collect, _final, _stage, scripted
    staged, src = _stage(title="Pasta", body=RECIPE)
    page = (f"---\ntitle: Pasta\nsummary: A recipe.\nsources:\n  - {src}\n---\n# Pasta\n\n"
            f"**Summary:** Boil spaghetti and add tomatoes [[source: {src}]].\n")
    with job_agent().override(model=scripted([[("write_page", {"name": "pasta", "content": page})],
                                              _final(report="ok")])):
        result, _ = await _collect(vault, "capture", {}, staged=staged)
    assert any("[[pasta]] names no protein" in w for w in result["warnings"])
