"""Run an agent and translate pydantic-ai stream events into small JSON events for the UI."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic_ai import Agent, AgentRunResultEvent, ModelHTTPError, UsageLimitExceeded, UsageLimits, UserError
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
)

from app.graph.network import GRAPH_TOOLS, from_tool_result
from app.llm import Role, connection_hint, get_model, model_settings

PREVIEW_CHARS = 1500


def _preview(content: Any) -> str:
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + " ..."


async def _graph_event(tool: str, content: Any) -> dict | None:
    """A network for the chat UI from a graph tool's full result (not the short preview).
    Never fails the run: a result that can't be drawn just yields no graph."""
    try:
        data = json.loads(content) if isinstance(content, str) else content
        return await asyncio.to_thread(from_tool_result, tool, data)
    except Exception:  # noqa: BLE001 - truncated JSON, database hiccup...
        return None


class JobFailed(Exception):
    """A job step failed in a way the user should read as-is (the vault is rolled back)."""


def explain_error(e: Exception) -> str:
    if isinstance(e, JobFailed):
        return str(e)
    if isinstance(e, ModelHTTPError):
        if e.status_code == 429:
            detail = e.body.get("error", {}).get("message", "") if isinstance(e.body, dict) else ""
            return ("The model provider's rate limit was reached (HTTP 429) even after waiting. "
                    "If a single request is larger than the per-minute limit, waiting can't help: "
                    "lower TOOL_OUTPUT_CHARS, or switch MODEL to one with a higher limit."
                    + (f"\nProvider says: {detail.split(' Need more tokens?')[0]}" if detail else ""))
        if e.status_code in (401, 403):
            return f"The model provider rejected the API key (HTTP {e.status_code}). Check .env."
        if e.status_code == 404:
            return ("The model server says this model doesn't exist (HTTP 404). Check the model "
                    "id; for Ollama, pull it first (ollama pull <model>). "
                    f"{str(e.body)[:200]}")
        return f"Model provider error (HTTP {e.status_code}): {str(e.body)[:300]}"
    if isinstance(e, UsageLimitExceeded):
        return f"Stopped: the agent used too many steps ({e})."
    hint = connection_hint(e)
    if hint:
        return hint
    if isinstance(e, UserError):
        m = re.search(r"Set the `([A-Z0-9_]+)` environment variable", str(e))
        if m:
            return f"The model needs {m.group(1)}: add it to .env (or {m.group(1)}_FILE for a secret file)."
        return str(e)
    return f"{type(e).__name__}: {e}"


async def stream_run(agent: Agent, prompt: str, *, deps: Any, request_limit: int,
                     history: Sequence[ModelMessage] | None = None,
                     model: str | None = None, role: Role = "chat") -> AsyncIterator[dict]:
    """Yield UI events; the last one is {"type": "result", "output", "messages", "usage"}."""
    async with agent.run_stream_events(
        prompt, deps=deps, message_history=list(history or []), model=get_model(model, role),
        model_settings=model_settings(), usage_limits=UsageLimits(request_limit=request_limit),
    ) as events:
        async for ev in events:
            if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart) and ev.part.content:
                yield {"type": "text", "delta": ev.part.content}
            elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                if ev.delta.content_delta:
                    yield {"type": "text", "delta": ev.delta.content_delta}
            elif isinstance(ev, FunctionToolCallEvent):
                yield {"type": "tool_call", "id": ev.part.tool_call_id, "name": ev.part.tool_name,
                       "args": ev.part.args_as_dict()}
            elif isinstance(ev, FunctionToolResultEvent):
                part = ev.part
                retry = isinstance(part, RetryPromptPart)
                yield {"type": "tool_result", "id": part.tool_call_id, "name": part.tool_name,
                       "retry": retry,
                       "content": _preview(part.model_response() if retry else part.content)}
                if not retry and part.tool_name in GRAPH_TOOLS:
                    graph = await _graph_event(part.tool_name, part.content)
                    if graph:
                        yield {"type": "graph", "id": part.tool_call_id, "tool": part.tool_name, **graph}
            elif isinstance(ev, AgentRunResultEvent):
                usage = ev.result.usage
                yield {"type": "result", "output": ev.result.output,
                       "messages": ev.result.all_messages(),
                       "usage": {"requests": usage.requests, "input_tokens": usage.input_tokens,
                                 "output_tokens": usage.output_tokens}}
