"""Saved conversations and the running summary (offline: scripted models, no graph lookups)."""

from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.chat import chat_agent
from app.agents.summary import summary_agent
from app.chat_store import ChatStore, chat_store
from app.config import get_settings


@pytest.fixture(autouse=True)
def no_prefetch(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_prefetch", False)


def _model(calls: list, fail: bool = False):
    """Answers 'answer-<n>', where n counts the user prompts the model was sent (history + this one)."""
    def fn(messages, info: AgentInfo):
        n = sum(1 for m in messages if isinstance(m, ModelRequest)
                for p in m.parts if p.part_kind == "user-prompt")
        calls.append({"n": n, "instructions": info.instructions or ""})
        if fail:
            raise RuntimeError("boom")
        return ModelResponse(parts=[TextPart(f"answer-{n}")])

    async def stream(messages, info):
        yield fn(messages, info).parts[0].content
    return FunctionModel(fn, stream_function=stream)


def _chat(client, message, session=None, vault="default"):
    body = {"message": message, **({"session_id": session} if session else {})}
    with client.stream("POST", f"/api/vaults/{vault}/chat", json=body) as r:
        raw = r.read().decode()
    events = {}
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.setdefault(lines["event"], []).append(json.loads(lines["data"]))
    return events["session"][0]["session_id"], "".join(e["delta"] for e in events.get("text", [])), events


def test_conversation_is_saved_and_continued(make_client, auth_db):
    c, calls = make_client("user", username="alice"), []
    with chat_agent().override(model=_model(calls)):
        sid, text, events = _chat(c, "What phosphorylates TDP1?")
        assert text == "answer-1" and events["session"][0]["title"] == "What phosphorylates TDP1?"
        assert _chat(c, "And ATM?", sid)[1] == "answer-2"  # the first turn is in the history
    listed = c.get("/api/vaults/default/conversations").json()
    assert [(x["id"], x["turns"]) for x in listed] == [(sid, 2)]

    # A fresh store on the same file (as after a restart) has everything.
    store = ChatStore(auth_db.path)
    conv = store.get(sid, auth_db.list_users()[0].id, "default")
    turns = store.turns(conv)
    assert [(t.question, t.answer) for t in turns] == [("What phosphorylates TDP1?", "answer-1"), ("And ATM?", "answer-2")]
    assert turns[1].ui["usage"]["requests"] == 1
    assert all(m.instructions is None for t in turns for m in t.messages if isinstance(m, ModelRequest))
    store.close()

    detail = c.get(f"/api/vaults/default/conversations/{sid}").json()
    assert [t["answer"] for t in detail["turns"]] == ["answer-1", "answer-2"] and detail["summary"] == ""


def test_conversations_are_private(make_client, auth_db):
    from app.api.deps import registry
    alice, bob = make_client("user", username="alice"), make_client("user", username="bobby")
    admin = make_client("admin", username="boss")
    registry().create("other")
    with chat_agent().override(model=_model([])):
        sid = _chat(alice, "first")[0]
        url = f"/api/vaults/default/conversations/{sid}"
        for client in (bob, admin):  # nobody else, not even an admin
            assert client.get(url).status_code == 404
            assert client.patch(url, json={"title": "x"}).status_code == 404
            assert client.delete(url).status_code == 404
            assert client.get("/api/vaults/default/conversations").json() == []
        bob_sid, text, _ = _chat(bob, "hijack", sid)
        assert bob_sid != sid and text == "answer-1"  # a new conversation, empty history
        other_sid, text, _ = _chat(alice, "elsewhere", sid, vault="other")
        assert other_sid != sid and text == "answer-1"  # not continued in another vault
    assert alice.get(url).json()["turns"][0]["question"] == "first"
    assert alice.patch(url, json={"title": "  "}).status_code == 400
    assert alice.patch(url, json={"title": "ATM and TDP1"}).json()["title"] == "ATM and TDP1"

    alice_id = next(u.id for u in auth_db.list_users() if u.username == "alice")
    boss_id = next(u.id for u in auth_db.list_users() if u.username == "boss")
    auth_db.delete_user(alice_id, actor_id=boss_id)
    assert chat_store()._q("SELECT COUNT(*) AS n FROM conversations WHERE user_id = ?", alice_id)[0]["n"] == 0
    assert chat_store()._q("SELECT COUNT(*) AS n FROM chat_turns WHERE conversation_id = ?", sid)[0]["n"] == 0


def test_delete_conversation(make_client):
    c = make_client("user")
    with chat_agent().override(model=_model([])):
        sid = _chat(c, "first")[0]
    assert c.delete(f"/api/vaults/default/conversations/{sid}").status_code == 204
    assert c.get("/api/vaults/default/conversations").json() == []


def test_failed_turn_is_shown_but_not_in_the_history(make_client):
    c, calls = make_client("user"), []
    with chat_agent().override(model=_model(calls)):
        sid = _chat(c, "first")[0]
    with chat_agent().override(model=_model(calls, fail=True)):
        _, _, events = _chat(c, "breaks", sid)
        assert "boom" in events["error"][0]["message"]
    with chat_agent().override(model=_model(calls)):
        assert _chat(c, "third", sid)[1] == "answer-2"  # first + third, not the failed one
    turns = c.get(f"/api/vaults/default/conversations/{sid}").json()["turns"]
    assert [bool(t["error"]) for t in turns] == [False, True, False]


def test_without_summaries_only_the_window_is_kept(make_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_summarize", False)
    monkeypatch.setattr(get_settings(), "chat_keep_turns", 2)
    c, calls, summary_calls = make_client("user"), [], []
    with chat_agent().override(model=_model(calls)), summary_agent().override(model=_model(summary_calls)):
        sid = None
        for i in range(5):
            sid, text, _ = _chat(c, f"q{i}", sid)
    assert [x["n"] for x in calls] == [1, 2, 3, 3, 3] and not summary_calls


def test_older_turns_are_summarized(make_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_keep_turns", 2)
    monkeypatch.setattr(get_settings(), "chat_summary_chars", 100)
    prompts = []

    def summarize(messages, info: AgentInfo):
        prompts.append(messages[-1].parts[-1].content)
        return ModelResponse(parts=[TextPart("They discussed ATM and TDP1. " * 20)])
    c, calls = make_client("user"), []
    with chat_agent().override(model=_model(calls)), summary_agent().override(model=FunctionModel(summarize)):
        sid = _chat(c, "q1 about ATM")[0]
        _chat(c, "q2 about TDP1", sid)
        assert not prompts  # the window (2 turns) isn't full yet
        _chat(c, "q3", sid)  # 3 > 2: fold all but the newest one into the summary
        assert len(prompts) == 1 and "User: q1 about ATM" in prompts[0] and "User: q2 about TDP1" in prompts[0]
        assert "User: q3" not in prompts[0]
        detail = c.get(f"/api/vaults/default/conversations/{sid}").json()
        assert detail["summarized_upto"] == 2 and len(detail["summary"]) == 100
        assert _chat(c, "q4", sid)[1] == "answer-2"  # q3 + q4; q1 and q2 come through the summary
    assert "# Earlier in this conversation (summary)\nThey discussed ATM" in calls[-1]["instructions"]
    assert "# Earlier in this conversation (summary)" not in calls[0]["instructions"]


def test_a_failed_summary_keeps_the_turns(make_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_keep_turns", 2)

    def broken(messages, info):
        raise RuntimeError("summary model down")
    c, calls = make_client("user"), []
    with chat_agent().override(model=_model(calls)), summary_agent().override(model=FunctionModel(broken)):
        sid = None
        for i in range(6):
            sid, text, events = _chat(c, f"q{i}", sid)
            assert "error" not in events
    # Nothing summarized, so the history grows up to twice the window instead of losing turns.
    assert [x["n"] for x in calls] == [1, 2, 3, 4, 5, 5]
    detail = c.get(f"/api/vaults/default/conversations/{sid}").json()
    assert detail["summary"] == "" and detail["summarized_upto"] == 0


def test_old_conversations_are_deleted_at_startup(make_client, auth_db, monkeypatch):
    user = auth_db.create_user("olduser", "password123")
    store = chat_store()
    old, recent = store.create(user.id, "default", "old"), store.create(user.id, "default", "recent")
    store._q("UPDATE conversations SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", old.id)
    monkeypatch.setattr(get_settings(), "chat_retention_days", 30)
    make_client(None)  # starting the app runs the clean-up
    assert store.get(old.id, user.id, "default") is None
    assert store.get(recent.id, user.id, "default") is not None
