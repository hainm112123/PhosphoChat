"""Chat agent: answers from the wiki and the graph, read-only, with citations."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from functools import lru_cache

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from app import prompts
from app.config import get_settings
from app.tools.agent_tools import Deps, graph_toolset, skill_toolset, vault_read_toolset

CHAT_RULES = """\
# Your role in this conversation
You are PhosphoChat, the question-answering assistant for this vault. You combine the user's
wiki (their notes and sources) with the kinase-substrate databases (PhosphoSIGNOR + KiNet);
you cannot change the vault.

- Each turn may start with "Retrieved context": what the wiki, the user's recorded claims and
  the databases say about the proteins in the question. Use it first; call tools only for more
  detail (read_page, regulators_of, get_interaction, vault_claims...). If there is none, use the tools.
  Don't re-read pages or sources the context already quotes unless you need a detail it lacks:
  every extra call makes the request bigger, and small models have tight token limits.
- Answer only from that context and tool results; if neither has it, say so. Don't guess.
- Structure the answer in up to three short parts, leaving out a part that would be empty:
  **From your wiki** - what the user's pages say, citing [[page-name]] / [[source: file.md]].
  **From the databases** - cite (SIGNOR-277418; PMID ...) or (PhosphoSitePlus; PMID ...); say how
  many databases agree when it matters.
  **How they fit** - agreement and gaps: claims marked NEW are reported only by the user's
  source ("not in any database yet"); CONFLICTING ones disagree with SIGNOR; well-supported
  database facts missing from the wiki can be suggested for capture. Mention "unreviewed" claims as such.
  When one side has nothing, say so in one line (e.g. "Your wiki has nothing on TDP1 yet.").
- For a pure database question with nothing in the wiki, a short answer with a table is fine.
- Keep answers short; use a small table for lists of enzymes/targets.
- load_skill("query") has the detailed answering rules if you need them.
- Never write code, scripts or Cypher.
"""


def _instructions(ctx: RunContext[Deps]) -> str:
    return f"{prompts.schema()}\n\n{CHAT_RULES}"


@lru_cache
def chat_agent() -> Agent[Deps, str]:
    return Agent(
        deps_type=Deps,
        name="phosphochat-chat",
        instructions=_instructions,
        toolsets=[vault_read_toolset(), graph_toolset(), skill_toolset()],
        defer_model_check=True,
    )


NO_TOOLS_NOTE = """\
# This answer
Tools are not available for this answer: answer from the retrieved context only, and say
briefly when the context doesn't cover something.
"""


@lru_cache
def chat_agent_without_tools() -> Agent[Deps, str]:
    """Fallback when the model keeps inventing tools: same rules, no tools, retrieved context only."""
    return Agent(
        deps_type=Deps,
        name="phosphochat-chat-notools",
        instructions=[_instructions, NO_TOOLS_NOTE],
        defer_model_check=True,
    )


# --------------------------------------------------------------------------- sessions

class ChatSessions:
    """In-memory conversation history per (user, vault, session), trimmed to whole turns.
    Keyed by user so one user can't continue or read another's conversation."""

    def __init__(self, max_sessions: int = 500):
        self._data: OrderedDict[tuple[int, str, str], list[ModelMessage]] = OrderedDict()
        self.max_sessions = max_sessions

    def get(self, user_id: int, vault: str, session: str) -> list[ModelMessage]:
        return list(self._data.get((user_id, vault, session), []))

    def set(self, user_id: int, vault: str, session: str, messages: list[ModelMessage]) -> None:
        key = (user_id, vault, session)
        # Requests carry the instructions they were sent with, including the retrieved context.
        # Those are rebuilt every turn, so don't keep them in the stored conversation.
        messages = [replace(m, instructions=None) if isinstance(m, ModelRequest) and m.instructions else m
                    for m in messages]
        self._data[key] = _trim(messages, get_settings().chat_history_messages)
        self._data.move_to_end(key)
        while len(self._data) > self.max_sessions:
            self._data.popitem(last=False)

    def clear(self, user_id: int, vault: str, session: str) -> None:
        self._data.pop((user_id, vault, session), None)


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
