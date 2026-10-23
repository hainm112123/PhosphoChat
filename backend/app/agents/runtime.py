"""Run an agent and translate pydantic-ai stream events into small JSON events for the UI."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic_ai import Agent, AgentRunResultEvent, ModelHTTPError, UsageLimitExceeded, UsageLimits
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

from app.llm import get_model, model_settings

PREVIEW_CHARS = 1500


def _preview(content: Any) -> str:
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + " ..."


def explain_error(e: Exception) -> str:
    if isinstance(e, ModelHTTPError):
        if e.status_code == 429:
            detail = e.body.get("error", {}).get("message", "") if isinstance(e.body, dict) else ""
            return ("The model provider's rate limit was reached (HTTP 429) even after waiting. "
                    "If a single request is larger than the per-minute limit, waiting can't help: "
                    "lower TOOL_OUTPUT_CHARS, or switch MODEL to one with a higher limit."
                    + (f"\nProvider says: {detail.split(' Need more tokens?')[0]}" if detail else ""))
        if e.status_code in (401, 403):
            return f"The model provider rejected the API key (HTTP {e.status_code}). Check .env."
        return f"Model provider error (HTTP {e.status_code}): {str(e.body)[:300]}"
    if isinstance(e, UsageLimitExceeded):
        return f"Stopped: the agent used too many steps ({e})."
    return f"{type(e).__name__}: {e}"


async def stream_run(agent: Agent, prompt: str, *, deps: Any, request_limit: int,
                     history: Sequence[ModelMessage] | None = None,
                     model: str | None = None) -> AsyncIterator[dict]:
    """Yield UI events; the last one is {"type": "result", "output", "messages", "usage"}."""
    async with agent.run_stream_events(
        prompt, deps=deps, message_history=list(history or []), model=get_model(model),
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
            elif isinstance(ev, AgentRunResultEvent):
                usage = ev.result.usage
                yield {"type": "result", "output": ev.result.output,
                       "messages": ev.result.all_messages(),
                       "usage": {"requests": usage.requests, "input_tokens": usage.input_tokens,
                                 "output_tokens": usage.output_tokens}}
