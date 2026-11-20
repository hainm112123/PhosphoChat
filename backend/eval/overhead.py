"""Fixed prompt overhead per agent, as the provider counts it.

    cd backend && .venv/Scripts/python -m eval.overhead --out overhead-before.json [--groq-key alternative]

For each agent, a FunctionModel captures exactly what pydantic-ai would send (instructions, tool
definitions, output tool); then one minimal request with that content goes to Groq
(`openai/gpt-oss-20b`, max 1 output token) and the reported prompt_tokens is recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import tempfile
from pathlib import Path

import httpx
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from eval.common import RESULTS, add_groq_key_arg, save, use_groq_key

GROQ_MODEL = "openai/gpt-oss-20b"


def agents() -> dict:
    """name -> (agent, deps). Covers the agents of both the old and the new design."""
    from app.agents import chat, jobs, reader
    from app.knowledge.topic import guard_agent
    from app.tools.agent_tools import Deps
    from app.vault.store import VaultRegistry
    vault = VaultRegistry(Path(tempfile.mkdtemp())).create("t")
    out = {}
    if hasattr(jobs, "writer_agent"):  # old design: one job agent + a writer for long sources
        out["job (capture, direct reading)"] = (jobs.job_agent(), jobs.JobContext(vault=vault, kind="capture"))
        out["writer (capture/sync)"] = (jobs.writer_agent(), jobs.JobContext(vault=vault, kind="capture"))
        out["job (lint)"] = (jobs.job_agent(), jobs.JobContext(vault=vault, kind="lint"))
        out["job (digest)"] = (jobs.job_agent(), jobs.JobContext(vault=vault, kind="digest"))
    else:  # new design: one agent; each kind gets its own skill and tools
        for kind in ("capture", "lint", "digest"):
            label = "writer (capture/sync)" if kind == "capture" else f"job ({kind})"
            out[label] = (jobs.job_agent(), jobs.JobContext(vault=vault, kind=kind))
    out["chat"] = (chat.chat_agent(), Deps(vault=vault))
    out["chat (no tools)"] = (chat.chat_agent_without_tools(), Deps(vault=vault))
    out["reader (one part)"] = (reader.reader_agent(), None)
    out["topic guard"] = (guard_agent(), None)
    return out


async def capture(agent, deps) -> dict:
    seen = {}

    def fn(messages, info: AgentInfo):
        seen["instructions"] = info.instructions or ""
        seen["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                           "parameters": t.parameters_json_schema}}
                         for t in [*info.function_tools, *info.output_tools]]
        if info.output_tools:
            raise RuntimeError("captured")
        return ModelResponse(parts=[TextPart("x")])
    with agent.override(model=FunctionModel(fn)):
        try:
            await (agent.run("hi", deps=deps) if deps is not None else agent.run("hi"))
        except Exception:  # noqa: BLE001 - we only wanted the request
            pass
    return seen


def measure(seen: dict) -> int:
    body = {"model": GROQ_MODEL, "max_tokens": 1,
            "messages": [{"role": "system", "content": seen["instructions"]}, {"role": "user", "content": "hi"}]}
    if seen["tools"]:
        body["tools"] = seen["tools"]
    for _ in range(6):  # per-minute budget: wait it out
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=body, timeout=60,
                       headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"})
        if r.status_code != 429:
            break
        time.sleep(float(r.headers.get("retry-after", 20)) + 1)
    if r.status_code != 200:
        return -r.status_code
    return r.json()["usage"]["prompt_tokens"]


async def main_async(out_name: str) -> None:
    rows = {}
    for name, (agent, deps) in agents().items():
        seen = await capture(agent, deps)
        chars_i, chars_t = len(seen.get("instructions", "")), len(json.dumps(seen.get("tools", [])))
        tokens = measure(seen) if seen else None
        rows[name] = {"instructions_chars": chars_i, "tool_definitions_chars": chars_t,
                      "tools": len(seen.get("tools", [])), "prompt_tokens": tokens}
        print(f"{name:30} instructions {chars_i:6d}  tools {chars_t:6d} ({rows[name]['tools']:2d})  "
              f"prompt_tokens {tokens}", flush=True)
    save(RESULTS / out_name, rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="overhead.json")
    add_groq_key_arg(ap)
    a = ap.parse_args()
    use_groq_key(a.groq_key)
    asyncio.run(main_async(a.out))


if __name__ == "__main__":
    main()
