"""Streaming chat over the wiki and the graph (SSE over a POST). Any logged-in user may chat;
each user's conversations are kept apart.

Before the model runs, the question's proteins are looked up in the vault and the graph
(knowledge/context.py); that context is given to the model for this turn only, and streamed
to the UI as a `context` event plus a `graph` event with the network behind it.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette import EventSourceResponse

from app.agents.chat import chat_agent, chat_agent_without_tools, sessions
from app.agents.runtime import explain_error, is_bad_tool_call, is_too_large, stream_run
from app.api.deps import get_vault
from app.auth.deps import current_user
from app.auth.store import User
from app.config import get_settings
from app.graph import network
from app.knowledge.context import build_context
from app.tools.agent_tools import Deps

router = APIRouter(prefix="/api/vaults/{vault}/chat")

# Groq's free tier counts tokens per minute; after a request is refused as too large, this is
# roughly how long until enough of the minute's budget is free again for a small request.
BUDGET_WAIT_SECONDS = 30


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None
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


@router.post("")
async def chat(vault: str, body: ChatIn, user: User = Depends(current_user)):
    v = get_vault(vault)
    session = body.session_id or uuid.uuid4().hex[:12]
    model = body.model if user.is_admin else None

    async def gen():
        yield {"event": "session", "data": json.dumps({"session_id": session})}
        summary, context_text, net = await asyncio.to_thread(_prefetch, v, body.message)
        if summary:
            yield {"event": "context", "data": json.dumps(summary)}
        if net:
            yield {"event": "graph", "data": json.dumps({"type": "graph", "tool": "context", **net}, default=str)}
        # Attempt 1 uses the tools. If the provider rejects a tool the model made up, or the
        # request outgrows the model's token limit, start the answer over: without tools when
        # there is retrieved context to answer from (a small request), otherwise with the tools
        # again. A fallback that is itself refused as too large usually means the first attempt
        # used up this minute's token budget, so wait for it and try once more.
        fallback = chat_agent_without_tools() if context_text else chat_agent()
        agents = [chat_agent(), fallback, fallback]
        for attempt, agent in enumerate(agents):
            try:
                async for ev in stream_run(agent, body.message, deps=Deps(vault=v),
                                           history=sessions.get(user.id, vault, session),
                                           request_limit=get_settings().chat_request_limit,
                                           model=model, role="chat", extra_instructions=context_text):
                    if ev["type"] == "result":
                        sessions.set(user.id, vault, session, ev["messages"])
                        yield {"event": "done", "data": json.dumps({"usage": ev["usage"]})}
                    else:
                        yield {"event": ev["type"], "data": json.dumps(ev, default=str)}
                return
            except Exception as e:  # noqa: BLE001 - shown in the chat
                too_large = is_too_large(e) and bool(context_text)
                if attempt == 0 and (is_bad_tool_call(e) or too_large):
                    why = ("the request grew past the model's token limit" if too_large
                           else "the model called a tool that doesn't exist")
                    note = f"Answering again{' from the retrieved context' if context_text else ''}: {why}."
                    yield {"event": "reset", "data": json.dumps({"message": note})}
                    continue
                if attempt == 1 and too_large:
                    yield {"event": "reset", "data": json.dumps({"message": (
                        f"Waiting {BUDGET_WAIT_SECONDS} s for the model's per-minute token budget, then answering "
                        "from the retrieved context.")})}
                    await asyncio.sleep(BUDGET_WAIT_SECONDS)
                    continue
                yield {"event": "error", "data": json.dumps({"message": explain_error(e)})}
                return

    return EventSourceResponse(gen())


@router.delete("/{session_id}", status_code=204)
def reset(vault: str, session_id: str, user: User = Depends(current_user)) -> None:
    get_vault(vault)
    sessions.clear(user.id, vault, session_id)
