"""Running summary of a long chat, so turns that leave the history window aren't forgotten.

Once a conversation has more than CHAT_KEEP_TURNS turns the summary doesn't cover, the oldest
ones (all but the newest half of the window) are folded into the summary. Only the question
and answer text goes to the model, never tool outputs, so the request stays small. A failure
leaves the summary as it was; those turns stay in the history and are tried again next turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from functools import lru_cache

from pydantic_ai import Agent

from app.chat_store import ChatStore, Conversation, Turn
from app.config import get_settings
from app.llm import get_model, model_settings

log = logging.getLogger("phosphochat")

ANSWER_CHARS = 1500  # of each answer, in the summary's input

SUMMARY_RULES = """\
You keep a running summary of a conversation between a researcher and PhosphoChat, an
assistant that answers from the researcher's wiki and kinase-substrate databases.

Update the summary with the new turns. Keep what later questions may refer to: the proteins,
sites and relations discussed, the citations given ([[page]], [[source: ...]], SIGNOR ids,
PMIDs), conclusions, open questions, and anything the user asked for or decided. Drop
greetings and repetition. Add nothing that isn't in the turns. Write plain prose or short
bullets, at most {chars} characters. Reply with the summary only.
"""

_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def lock(conversation_id: str) -> asyncio.Lock:
    """Held while a conversation's summary is updated; a chat turn takes it before reading
    the history, so it never sees turns half-way into the summary."""
    return _locks[conversation_id]


@lru_cache
def summary_agent() -> Agent[None, str]:
    return Agent(name="phosphochat-summary", defer_model_check=True)


def due(store: ChatStore, conv: Conversation) -> list[Turn]:
    """The turns to fold into the summary now (empty when the window isn't full yet)."""
    keep = max(1, get_settings().chat_keep_turns)
    turns = store.unsummarized(conv)
    if not get_settings().chat_summarize or len(turns) <= keep:
        return []
    return turns[:len(turns) - (keep + 1) // 2]


def _prompt(previous: str, turns: list[Turn]) -> str:
    parts = [f"Summary so far:\n{previous or '(none yet)'}", "New turns:"]
    for t in turns:
        answer = t.answer if len(t.answer) <= ANSWER_CHARS else t.answer[:ANSWER_CHARS] + " ..."
        parts.append(f"User: {t.question}\nAssistant: {answer}")
    return "\n\n".join(parts)


async def summarize(store: ChatStore, conv: Conversation) -> bool:
    """Fold the due turns into the summary. Returns whether it changed; never raises."""
    async with lock(conv.id):
        conv = store.get(conv.id, conv.user_id, conv.vault) or conv  # re-read under the lock
        turns = due(store, conv)
        if not turns:
            return False
        chars = get_settings().chat_summary_chars
        try:
            result = await summary_agent().run(
                _prompt(conv.summary, turns), instructions=SUMMARY_RULES.format(chars=chars),
                model=get_model(None, "chat"), model_settings=model_settings())
        except Exception as e:  # noqa: BLE001 - the chat works without it; retried next turn
            log.warning("Chat summary failed for conversation %s: %s", conv.id, e)
            return False
        text = result.output.strip()
        if not text:
            return False
        store.set_summary(conv, text[:chars], turns[-1].seq)
        return True


def summary_instructions(summary: str) -> str | None:
    return f"# Earlier in this conversation (summary)\n{summary}" if summary else None
