"""Fixed prompt overhead per agent: instructions + tool definitions, sent with every request.

Groq's free tier allows 8,000 tokens per request, so these budgets (characters; roughly 4 per
token) keep room for the content. Raise one only on purpose, after measuring with
`python -m eval.overhead`.
"""

from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents import chat, jobs, reader
from app.knowledge.topic import guard_agent
from app.tools.agent_tools import Deps

BUDGETS = {  # name: (max characters, expected tools)
    "capture": (6500, {"read_index", "list_pages", "read_page", "write_page", "upsert_index"}),
    "sync": (6500, {"read_index", "list_pages", "read_page", "write_page", "upsert_index"}),
    "digest": (6500, {"read_index", "list_pages", "read_page", "write_page", "upsert_index"}),
    "lint": (7000, {"read_index", "list_pages", "read_page", "search_pages", "list_sources",
                    "write_page", "upsert_index"}),
    "chat": (5500, {"read_page", "search_pages", "vault_claims", "search_entities", "interactions",
                    "find_paths", "get_interaction"}),
    "chat-no-tools": (2500, set()),
    "reader": (2200, set()),
    "guard": (1500, set()),
}


async def _request(agent, deps) -> tuple[str, list[dict], set[str]]:
    seen = {}

    def fn(messages, info: AgentInfo):
        seen["instructions"] = info.instructions or ""
        seen["tools"] = [{"name": t.name, "description": t.description, "parameters": t.parameters_json_schema}
                         for t in [*info.function_tools, *info.output_tools]]
        seen["names"] = {t.name for t in info.function_tools}
        raise RuntimeError("captured")
    with agent.override(model=FunctionModel(fn)):
        with pytest.raises(RuntimeError, match="captured"):
            await (agent.run("hi", deps=deps) if deps is not None else agent.run("hi"))
    return seen["instructions"], seen["tools"], seen["names"]


@pytest.mark.parametrize("name", list(BUDGETS))
async def test_fixed_overhead_stays_within_budget(name, vault):
    agent, deps = {
        "chat": (chat.chat_agent(), Deps(vault=vault)),
        "chat-no-tools": (chat.chat_agent_without_tools(), Deps(vault=vault)),
        "reader": (reader.reader_agent(), None),
        "guard": (guard_agent(), None),
    }.get(name) or (jobs.job_agent(), jobs.JobContext(vault=vault, kind=name))
    instructions, tools, names = await _request(agent, deps)
    limit, expected = BUDGETS[name]
    size = len(instructions) + len(json.dumps(tools))
    assert size <= limit, f"{name}: {size} characters of fixed prompt (budget {limit})"
    assert names == expected


async def test_each_job_kind_gets_its_own_skill(vault):
    lint, _, _ = await _request(jobs.job_agent(), jobs.JobContext(vault=vault, kind="lint"))
    capture, _, _ = await _request(jobs.job_agent(), jobs.JobContext(vault=vault, kind="capture"))
    assert "Skill: lint" in lint and "Skill: lint" not in capture
    assert "reading notes" in capture and "Grounding" in capture
