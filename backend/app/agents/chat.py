"""Chat agent: answers from the wiki and the graph, read-only, with citations."""

from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from app.config import get_settings
from app.graph.signor import INSTRUCTIONS as GRAPH_INSTRUCTIONS
from app.tools.agent_tools import Deps, graph_toolset, skill_toolset, vault_read_toolset

CHAT_RULES = """\
# Your role in this conversation
You are PhosphoChat, the question-answering assistant for this vault. You can read the wiki
and sources and query the PhosphoSIGNOR graph; you cannot change the vault.

- Answer only from tool results; if the tools don't have it, say so. Don't guess.
- For graph questions call the matching graph tool first (one call is often enough).
- For questions about the user's notes, read_index then search_pages/read_page.
- Cite every fact: wiki pages as [[page-name]], sources as [[source: filename.md]],
  graph facts as (SIGNOR-277418; PMID 30419043).
- Keep answers short and structured; use a small table for lists of enzymes/targets.
- load_skill("query") has the detailed answering rules if you need them.
- Never write code, scripts or Cypher.
"""


def _instructions(ctx: RunContext[Deps]) -> str:
    agents_md = (ctx.deps.vault.root / "AGENTS.md")
    schema = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else ""
    return f"{schema}\n\n{GRAPH_INSTRUCTIONS}\n\n{CHAT_RULES}"


@lru_cache
def chat_agent() -> Agent[Deps, str]:
    return Agent(
        deps_type=Deps,
        name="phosphochat-chat",
        instructions=_instructions,
        toolsets=[vault_read_toolset(), graph_toolset(), skill_toolset()],
        defer_model_check=True,
    )


# --------------------------------------------------------------------------- sessions

class ChatSessions:
    """In-memory conversation history per (vault, session), trimmed to whole turns."""

    def __init__(self, max_sessions: int = 200):
        self._data: OrderedDict[tuple[str, str], list[ModelMessage]] = OrderedDict()
        self.max_sessions = max_sessions

    def get(self, vault: str, session: str) -> list[ModelMessage]:
        return list(self._data.get((vault, session), []))

    def set(self, vault: str, session: str, messages: list[ModelMessage]) -> None:
        self._data[(vault, session)] = _trim(messages, get_settings().chat_history_messages)
        self._data.move_to_end((vault, session))
        while len(self._data) > self.max_sessions:
            self._data.popitem(last=False)

    def clear(self, vault: str, session: str) -> None:
        self._data.pop((vault, session), None)


def _trim(messages: list[ModelMessage], max_messages: int) -> list[ModelMessage]:
    """Drop the oldest turns, cutting only at a user prompt so tool call/return pairs stay intact."""
    if len(messages) <= max_messages:
        return messages
    starts = [i for i, m in enumerate(messages)
              if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)]
    for i in starts:
        if len(messages) - i <= max_messages:
            return messages[i:]
    return messages[starts[-1]:] if starts else messages[-max_messages:]


sessions = ChatSessions()
