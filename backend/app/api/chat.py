"""Streaming chat over the wiki and the graph (SSE over a POST), and the saved conversations.
Any logged-in user may chat; each user sees only their own conversations.

Before the model runs, the question's proteins are looked up in the vault and the graph
(knowledge/context.py); that context is given to the model for this turn only, and streamed
to the UI as a `context` event plus a `graph` event with the network behind it.

Conversations are stored (chat_store.py): the model gets the last CHAT_KEEP_TURNS turns as they
were, plus a running summary of older ones (agents/summary.py).
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sse_starlette import EventSourceResponse

from app.agents import summary
from app.agents.chat import chat_agent, chat_agent_without_tools
from app.agents.runtime import explain_error, is_bad_tool_call, is_too_large, is_transient, stream_run
from app.api.deps import get_vault
from app.auth.deps import current_user
from app.auth.store import User
from app.chat_store import Conversation, chat_store
from app.config import get_settings
from app.graph import network
from app.knowledge import topic
from app.knowledge.context import build_context
from app.tools.agent_tools import Deps

router = APIRouter(prefix="/api/vaults/{vault}/chat")
conversations = APIRouter(prefix="/api/vaults/{vault}/conversations")

# Groq's free tier counts tokens per minute; after a request is refused as too large, this is
# roughly how long until enough of the minute's budget is free again for a small request.
BUDGET_WAIT_SECONDS = 30
BUSY_WAIT_SECONDS = 10


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None  # the conversation to continue; a new one when unknown
    model: str | None = None  # admins only; others always use CHAT_MODEL


def _prefetch(v, message: str) -> tuple[dict | None, str | None, dict | None]:
    """(context summary, context text, network) for this question; never raises."""
    s = get_settings()
    if not s.chat_prefetch:
        return None, None, None
    try:
        pack = build_context(v, message, s.chat_context_chars)
    except Exception:  # noqa: BLE001 - retrieval is a help, not a requirement
        return None, None, None
    net = None
    if pack.entities and not pack.graph_error:
        try:
            net = network.network([m.ids[0] for m in pack.entities], depth=1, limit=60,
                                  claims=pack.claims)
            if not net["edges"]:
                net = None
        except Exception:  # noqa: BLE001
            net = None
    return pack.summary(), pack.text, net


def _history_turns() -> int:
    """Turns of history sent as they were. With summaries on, turns wait for the summary to
    take them over, up to twice the window if the summary keeps failing."""
    keep = max(1, get_settings().chat_keep_turns)
    return 2 * keep if get_settings().chat_summarize else keep


class _Transcript:
    """What the chat page shows for a turn, rebuilt from the events sent (as ChatPage does)."""

    def __init__(self):
        self.text = ""
        self.ui: dict = {"tools": []}

    def add(self, ev: str, data: dict) -> None:
        if ev == "text":
            self.text += data["delta"]
        elif ev == "reset":
            self.text, self.ui["notice"] = "", data["message"]
        elif ev == "tool_call":
            self.ui["tools"].append({"id": data["id"], "name": data["name"], "args": data["args"]})
        elif ev == "tool_result":
            for t in self.ui["tools"]:
                if t["id"] == data["id"]:
                    t.update(result=data["content"], retry=data["retry"])
        elif ev == "context":
            self.ui["context"] = data
        elif ev == "graph":
            self.ui.setdefault("graphs", []).append({"nodes": data["nodes"], "edges": data["edges"]})
        elif ev == "done":
            self.ui["usage"] = data["usage"]
        elif ev == "off_topic":
            self.ui["refused"] = data["message"]


@router.post("")
async def chat(vault: str, body: ChatIn, user: User = Depends(current_user)):
    v = get_vault(vault)
    store = chat_store()
    conv = (body.session_id and store.get(body.session_id, user.id, vault)) \
        or store.create(user.id, vault, body.message)
    model = body.model if user.is_admin else None

    async def gen():
        log = _Transcript()

        def event(ev: str, data: dict) -> dict:
            log.add(ev, data)
            return {"event": ev, "data": json.dumps(data, default=str)}

        yield {"event": "session", "data": json.dumps({"session_id": conv.id, "title": conv.title})}
        verdict = await topic.check_question(body.message, store.last_question(conv))
        if not verdict.on_topic:  # no lookups, no chat model; kept out of the model's history
            reason = f" ({verdict.reason})" if verdict.reason else ""
            yield event("off_topic", {"message": topic.REFUSAL + reason})
            store.add_turn(conv, question=body.message, answer="", error=None, ui=log.ui, messages=[])
            return
        consulted, context_text, net = await asyncio.to_thread(_prefetch, v, body.message)
        async with summary.lock(conv.id):  # a summary still being written for the last turn
            fresh = store.get(conv.id, user.id, vault) or conv
            history = store.history(fresh, _history_turns())
        if consulted:
            yield event("context", consulted)
        if net:
            yield event("graph", {"type": "graph", "tool": "context", **net})
        instructions = "\n\n".join(filter(None, [summary.summary_instructions(fresh.summary), context_text])) or None
        messages, error = [], None
        # Attempt 1 uses the tools. If the provider rejects a tool the model made up, or the
        # request outgrows the model's token limit, start the answer over: without tools when
        # there is retrieved context to answer from (a small request), otherwise with the tools
        # again. A fallback that is itself refused as too large usually means the first attempt
        # used up this minute's token budget, so wait for it and try once more.
        # A provider that is briefly busy (503, per-minute 429) gets one retry of the same stage.
        fallback = chat_agent_without_tools() if context_text else chat_agent()
        stages, busy_retries = [(0, chat_agent()), (1, fallback), (2, fallback)], 1
        for i, (attempt, agent) in enumerate(stages):
            try:
                async for ev in stream_run(agent, body.message, deps=Deps(vault=v), history=history,
                                           request_limit=get_settings().chat_request_limit,
                                           model=model, role="chat", extra_instructions=instructions):
                    if ev["type"] == "result":
                        messages = ev["messages"][len(history):]
                        yield event("done", {"usage": ev["usage"]})
                    else:
                        yield event(ev["type"], ev)
                break
            except Exception as e:  # noqa: BLE001 - shown in the chat
                if busy_retries and is_transient(e):
                    busy_retries -= 1
                    what = f"HTTP {e.status_code}" if hasattr(e, "status_code") else "no response in time"
                    yield event("reset", {"message": f"The model provider is busy ({what}); "
                                                     f"trying again in {BUSY_WAIT_SECONDS} s."})
                    await asyncio.sleep(BUSY_WAIT_SECONDS)
                    stages.insert(i + 1, (attempt, agent))
                    continue
                too_large = is_too_large(e) and bool(context_text)
                if attempt == 0 and (is_bad_tool_call(e) or too_large):
                    why = ("the request grew past the model's token limit" if too_large
                           else "the model called a tool that doesn't exist")
                    note = f"Answering again{' from the retrieved context' if context_text else ''}: {why}."
                    yield event("reset", {"message": note})
                    continue
                if attempt == 1 and too_large:
                    yield event("reset", {"message": (
                        f"Waiting {BUDGET_WAIT_SECONDS} s for the model's per-minute token budget, then answering "
                        "from the retrieved context.")})
                    await asyncio.sleep(BUDGET_WAIT_SECONDS)
                    continue
                error = explain_error(e)
                yield event("error", {"message": error})
                break
        store.add_turn(conv, question=body.message, answer=log.text, error=error, ui=log.ui,
                       messages=[] if error else messages)
        if not error:
            # After `done`, so the answer isn't held up; the next turn waits for it (lock).
            await summary.summarize(store, conv)

    return EventSourceResponse(gen())


@router.delete("/{session_id}", status_code=204)
def reset(vault: str, session_id: str, user: User = Depends(current_user)) -> None:
    """Delete a conversation (kept for older clients; same as DELETE /conversations/{id})."""
    delete_conversation(vault, session_id, user)


# --------------------------------------------------------------------------- conversations

def _own(vault: str, cid: str, user: User) -> Conversation:
    get_vault(vault)
    conv = chat_store().get(cid, user.id, vault)
    if conv is None:  # someone else's id looks the same as a missing one
        raise HTTPException(404, "No such conversation.")
    return conv


@conversations.get("")
def list_conversations(vault: str, user: User = Depends(current_user)) -> list[dict]:
    get_vault(vault)
    return chat_store().list_for(user.id, vault)


@conversations.get("/{cid}")
def get_conversation(vault: str, cid: str, user: User = Depends(current_user)) -> dict:
    conv = _own(vault, cid, user)
    return {**conv.public(), "summary": conv.summary,
            "turns": [t.public() for t in chat_store().turns(conv)]}


class RenameIn(BaseModel):
    title: str


@conversations.patch("/{cid}")
def rename_conversation(vault: str, cid: str, body: RenameIn, user: User = Depends(current_user)) -> dict:
    try:
        return chat_store().rename(_own(vault, cid, user), body.title).public()
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@conversations.delete("/{cid}", status_code=204)
def delete_conversation(vault: str, cid: str, user: User = Depends(current_user)) -> None:
    chat_store().delete(_own(vault, cid, user))
