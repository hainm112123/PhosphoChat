"""Users and sessions in SQLite (stdlib only).

Passwords: scrypt with a per-user salt. Sessions: a random token in an HttpOnly cookie; only
its sha256 is stored, so a leaked database can't be used to log in.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

Role = Literal["user", "admin"]
USERNAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
MIN_PASSWORD = 8
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class AuthError(ValueError):
    """A request the caller can correct (bad input, guard violated)."""


class Conflict(AuthError):
    pass


@dataclass
class User:
    id: int
    username: str
    role: Role
    disabled: bool
    created_at: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role,
                "disabled": self.disabled, "created_at": self.created_at}


# --------------------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


# Spent on unknown usernames so a failed login takes the same time either way.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_username(username: str) -> str:
    u = username.strip().lower()
    if not (3 <= len(u) <= 32) or not set(u) <= USERNAME_CHARS:
        raise AuthError("Usernames are 3-32 characters: lowercase letters, digits, '.', '_' or '-'.")
    return u


def _check_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Passwords need at least {MIN_PASSWORD} characters.")
    return password


# --------------------------------------------------------------------------- store

class AuthStore:
    def __init__(self, path: Path, session_days: int = 14):
        self.path = path
        self.session_days = session_days
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
    def _user(row: sqlite3.Row) -> User:
        return User(row["id"], row["username"], row["role"], bool(row["disabled"]), row["created_at"])

    # ---- users

    def list_users(self) -> list[User]:
        return [self._user(r) for r in self._q("SELECT * FROM users ORDER BY username")]

    def get_user(self, user_id: int) -> User | None:
        rows = self._q("SELECT * FROM users WHERE id = ?", user_id)
        return self._user(rows[0]) if rows else None

    def has_admin(self) -> bool:
        return bool(self._q("SELECT 1 FROM users WHERE role = 'admin' AND disabled = 0 LIMIT 1"))

    def create_user(self, username: str, password: str, role: Role = "user") -> User:
        if role not in ("user", "admin"):
            raise AuthError("Role must be 'user' or 'admin'.")
        u, pw = _check_username(username), _check_password(password)
        try:
            self._q("INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                    u, hash_password(pw), role, _now().isoformat(timespec="seconds"))
        except sqlite3.IntegrityError:
            raise Conflict(f"A user named '{u}' already exists.") from None
        return self._user(self._q("SELECT * FROM users WHERE username = ?", u)[0])

    def _active_admins(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0")[0]["n"]

    def update_user(self, user_id: int, *, actor_id: int, role: Role | None = None,
                    password: str | None = None, disabled: bool | None = None) -> User:
        user = self.get_user(user_id)
        if not user:
            raise KeyError(user_id)
        losing_admin = user.is_admin and not user.disabled and (
            (role is not None and role != "admin") or disabled is True)
        if user_id == actor_id and disabled:
            raise Conflict("You can't disable your own account.")
        if losing_admin and self._active_admins() <= 1:
            raise Conflict("This is the last active admin; make another admin first.")
        if role is not None:
            if role not in ("user", "admin"):
                raise AuthError("Role must be 'user' or 'admin'.")
            self._q("UPDATE users SET role = ? WHERE id = ?", role, user_id)
        if password is not None:
            self._q("UPDATE users SET password_hash = ? WHERE id = ?",
                    hash_password(_check_password(password)), user_id)
            self.end_sessions(user_id)
        if disabled is not None:
            self._q("UPDATE users SET disabled = ? WHERE id = ?", int(disabled), user_id)
            if disabled:
                self.end_sessions(user_id)
        return self.get_user(user_id)  # type: ignore[return-value]

    def delete_user(self, user_id: int, *, actor_id: int) -> None:
        user = self.get_user(user_id)
        if not user:
            raise KeyError(user_id)
        if user_id == actor_id:
            raise Conflict("You can't delete your own account.")
        if user.is_admin and not user.disabled and self._active_admins() <= 1:
            raise Conflict("This is the last active admin; make another admin first.")
        self._q("DELETE FROM users WHERE id = ?", user_id)

    def change_own_password(self, user_id: int, current: str, new: str) -> None:
        rows = self._q("SELECT password_hash FROM users WHERE id = ?", user_id)
        if not rows or not verify_password(current, rows[0]["password_hash"]):
            raise AuthError("The current password is wrong.")
        self._q("UPDATE users SET password_hash = ? WHERE id = ?",
                hash_password(_check_password(new)), user_id)

    def ensure_admin(self, username: str, password: str) -> User | None:
        """Create the bootstrap admin when no active admin exists. Returns it, or None."""
        if not username or not password or self.has_admin():
            return None
        existing = self._q("SELECT * FROM users WHERE username = ?", username.strip().lower())
        if existing:  # the named user exists but isn't an active admin: promote and reset
            uid = existing[0]["id"]
            self._q("UPDATE users SET role = 'admin', disabled = 0, password_hash = ? WHERE id = ?",
                    hash_password(_check_password(password)), uid)
            return self.get_user(uid)
        return self.create_user(username, password, "admin")

    # ---- login / sessions

    def authenticate(self, username: str, password: str) -> User | None:
        rows = self._q("SELECT * FROM users WHERE username = ?", username.strip().lower())
        if not rows:
            verify_password(password, _DUMMY_HASH)
            return None
        row = rows[0]
        if not verify_password(password, row["password_hash"]) or row["disabled"]:
            return None
        return self._user(row)

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        self._q("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                _token_hash(token), user_id, now.isoformat(),
                (now + timedelta(days=self.session_days)).isoformat())
        return token

    def session_user(self, token: str) -> User | None:
        rows = self._q("""SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id
                          WHERE s.token_hash = ?""", _token_hash(token))
        if not rows:
            return None
        row = rows[0]
        if datetime.fromisoformat(row["expires_at"]) < _now() or row["disabled"]:
            self.end_session(token)
            return None
        return self._user(row)

    def end_session(self, token: str) -> None:
        self._q("DELETE FROM sessions WHERE token_hash = ?", _token_hash(token))

    def end_sessions(self, user_id: int) -> None:
        self._q("DELETE FROM sessions WHERE user_id = ?", user_id)


# --------------------------------------------------------------------------- login throttle

class LoginThrottle:
    """At most `limit` failed logins per (username, client) per `window` seconds."""

    def __init__(self, limit: int = 5, window: float = 300):
        self.limit, self.window = limit, window
        self._fails: dict[tuple[str, str], list[float]] = {}

    def _recent(self, key: tuple[str, str]) -> list[float]:
        cutoff = time.monotonic() - self.window
        fails = [t for t in self._fails.get(key, []) if t > cutoff]
        self._fails[key] = fails
        return fails

    def blocked(self, username: str, client: str) -> bool:
        return len(self._recent((username.lower(), client))) >= self.limit

    def failed(self, username: str, client: str) -> None:
        self._recent((username.lower(), client)).append(time.monotonic())

    def succeeded(self, username: str, client: str) -> None:
        self._fails.pop((username.lower(), client), None)
