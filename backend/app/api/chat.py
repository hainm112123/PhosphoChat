"""Streaming chat over the wiki and the graph (SSE over a POST)."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette import EventSourceResponse

from app.agents.chat import chat_agent, sessions
from app.agents.runtime import explain_error, stream_run
from app.api.deps import get_vault
from app.config import get_settings
from app.tools.agent_tools import Deps

router = APIRouter(prefix="/api/vaults/{vault}/chat")


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None
    model: str | None = None


@router.post("")
async def chat(vault: str, body: ChatIn):
    v = get_vault(vault)
    session = body.session_id or uuid.uuid4().hex[:12]

    async def gen():
        yield {"event": "session", "data": json.dumps({"session_id": session})}
        try:
            async for ev in stream_run(chat_agent(), body.message, deps=Deps(vault=v),
                                       history=sessions.get(vault, session),
                                       request_limit=get_settings().chat_request_limit,
                                       model=body.model):
                if ev["type"] == "result":
                    sessions.set(vault, session, ev["messages"])
                    yield {"event": "done", "data": json.dumps({"usage": ev["usage"]})}
                else:
                    yield {"event": ev["type"], "data": json.dumps(ev, default=str)}
        except Exception as e:  # noqa: BLE001 - shown in the chat
            yield {"event": "error", "data": json.dumps({"message": explain_error(e)})}

    return EventSourceResponse(gen())


@router.delete("/{session_id}", status_code=204)
def reset(vault: str, session_id: str) -> None:
    get_vault(vault)
    sessions.clear(vault, session_id)
