"""Chat conversations in SQLite, next to the users table (same file, own connection).

A conversation belongs to one user and one vault. Each turn keeps two things:
- `ui_json`: what the chat page needs to redraw the answer (context, graphs, tool calls...);
- `messages_json`: the turn's pydantic-ai messages, which become the model's history.
Failed turns are kept for the UI with an error and no messages, so they never reach the model.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelRequest

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vault TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    summarized_upto INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_by_owner ON conversations (user_id, vault, updated_at);
CREATE TABLE IF NOT EXISTS chat_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    error TEXT,
    ui_json TEXT NOT NULL DEFAULT '{}',
    messages_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_by_conversation ON chat_turns (conversation_id, seq);
"""

TITLE_CHARS = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_title(question: str) -> str:
    line = " ".join(question.split())
    return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS - 1].rstrip() + "…"


@dataclass
class Conversation:
    id: str
    user_id: int
    vault: str
    title: str
    summary: str
    summarized_upto: int
    created_at: str
    updated_at: str

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "created_at": self.created_at,
                "updated_at": self.updated_at, "summarized_upto": self.summarized_upto}


@dataclass
class Turn:
    seq: int
    question: str
    answer: str
    error: str | None
    ui: dict
    messages: list[ModelMessage]

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.messages)

    def public(self) -> dict:
        return {"seq": self.seq, "question": self.question, "answer": self.answer,
                "error": self.error, "ui": self.ui}


def _strip_instructions(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Requests carry the instructions they were sent with, including the retrieved context
    and the summary. Those are rebuilt every turn, so they aren't stored."""
    return [replace(m, instructions=None) if isinstance(m, ModelRequest) and m.instructions else m
            for m in messages]


class ChatStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    @staticmethod
    def _conv(row: sqlite3.Row) -> Conversation:
        return Conversation(row["id"], row["user_id"], row["vault"], row["title"], row["summary"],
                            row["summarized_upto"], row["created_at"], row["updated_at"])

    @staticmethod
    def _turn(row: sqlite3.Row) -> Turn:
        raw = row["messages_json"]
        return Turn(row["seq"], row["question"], row["answer"], row["error"], json.loads(row["ui_json"]),
                    ModelMessagesTypeAdapter.validate_json(raw) if raw else [])

    # ---- conversations (every lookup checks the owner and the vault)

    def create(self, user_id: int, vault: str, title: str) -> Conversation:
        cid, now = secrets.token_hex(8), _now()
        self._q("INSERT INTO conversations (id, user_id, vault, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", cid, user_id, vault, make_title(title) or "New chat", now, now)
        return self.get(cid, user_id, vault)  # type: ignore[return-value]

    def get(self, cid: str, user_id: int, vault: str) -> Conversation | None:
        rows = self._q("SELECT * FROM conversations WHERE id = ? AND user_id = ? AND vault = ?",
                       cid, user_id, vault)
        return self._conv(rows[0]) if rows else None

    def list_for(self, user_id: int, vault: str) -> list[dict]:
        rows = self._q("""SELECT c.*, (SELECT COUNT(*) FROM chat_turns t WHERE t.conversation_id = c.id) AS turns
                          FROM conversations c WHERE c.user_id = ? AND c.vault = ?
                          ORDER BY c.updated_at DESC, c.created_at DESC""", user_id, vault)
        return [{**self._conv(r).public(), "turns": r["turns"]} for r in rows]

    def rename(self, conv: Conversation, title: str) -> Conversation:
        title = make_title(title)
        if not title:
            raise ValueError("A title can't be empty.")
        self._q("UPDATE conversations SET title = ? WHERE id = ?", title, conv.id)
        return replace(conv, title=title)

    def delete(self, conv: Conversation) -> None:
        self._q("DELETE FROM conversations WHERE id = ?", conv.id)

    def count_vault(self, vault: str) -> int:
        return self._q("SELECT COUNT(*) AS n FROM conversations WHERE vault = ?", vault)[0]["n"]

    def delete_vault(self, vault: str) -> int:
        """Every user's conversations in a vault (the vault was deleted). Returns how many."""
        with self._lock:
            return self._db.execute("DELETE FROM conversations WHERE vault = ?", (vault,)).rowcount

    def delete_older_than(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        n = self._q("SELECT COUNT(*) AS n FROM conversations WHERE updated_at < ?", cutoff)[0]["n"]
        self._q("DELETE FROM conversations WHERE updated_at < ?", cutoff)
        return n

    # ---- turns

    def add_turn(self, conv: Conversation, *, question: str, answer: str, error: str | None,
                 ui: dict, messages: list[ModelMessage]) -> int:
        with self._lock:
            seq = self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_turns WHERE conversation_id = ?",
                                   (conv.id,)).fetchone()[0]
            data = ModelMessagesTypeAdapter.dump_json(_strip_instructions(messages)).decode() if messages else ""
            self._db.execute("""INSERT INTO chat_turns (conversation_id, seq, question, answer, error, ui_json,
                                    messages_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                             (conv.id, seq, question, answer, error, json.dumps(ui, default=str),
                              data, _now()))
            self._db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), conv.id))
        return seq

    def turns(self, conv: Conversation) -> list[Turn]:
        return [self._turn(r) for r in self._q(
            "SELECT * FROM chat_turns WHERE conversation_id = ? ORDER BY seq", conv.id)]

    def unsummarized(self, conv: Conversation) -> list[Turn]:
        """Successful turns that the summary doesn't cover yet, oldest first."""
        rows = self._q("""SELECT * FROM chat_turns WHERE conversation_id = ? AND seq > ?
                          AND error IS NULL AND messages_json != '' ORDER BY seq""",
                       conv.id, conv.summarized_upto)
        return [self._turn(r) for r in rows]

    def history(self, conv: Conversation, max_turns: int) -> list[ModelMessage]:
        """The model's history: the newest `max_turns` turns the summary doesn't cover, whole
        (a turn never loses its tool call/return pairs)."""
        turns = self.unsummarized(conv)[-max_turns:] if max_turns > 0 else []
        return [m for t in turns for m in t.messages]

    def set_summary(self, conv: Conversation, summary: str, upto_seq: int) -> None:
        self._q("UPDATE conversations SET summary = ?, summarized_upto = ? WHERE id = ?",
                summary, upto_seq, conv.id)


_store: ChatStore | None = None


def chat_store() -> ChatStore:
    """The store in the same database file as the users (so deleting a user deletes their chats)."""
    global _store
    from app.auth.deps import auth_store
    path = auth_store().path
    if _store is None or _store.path != path:
        if _store is not None:
            _store.close()
        _store = ChatStore(path)
    return _store
