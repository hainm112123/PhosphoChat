"""Settings from the repo-root .env.

The .env is also loaded into os.environ so provider SDKs (GROQ_API_KEY, ANTHROPIC_API_KEY,
GOOGLE_API_KEY, OPENAI_API_KEY / OPENAI_BASE_URL) find their keys without extra wiring.

.env is re-read whenever the file changes, so editing MODEL (or anything else) takes effect
on the next request without restarting: `uvicorn --reload` only watches .py files, and .env
lives outside backend/. .env wins over the process environment for the keys it defines.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"

# The environment as it was before .env was applied. .env wins for every key it defines:
# VS Code's "python.terminal.useEnvFile" copies .env into a terminal when it opens, so the
# environment can hold stale copies of .env values. Keys .env doesn't define come from here.
_ORIGINAL_ENV = dict(os.environ)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    model: str = "groq:qwen/qwen3.8-27b"
    # Optional per-role models; empty means "use MODEL" (verify falls back to the job model).
    chat_model: str = ""
    job_model: str = ""
    verify_model: str = ""
    guard_model: str = ""              # topic check; empty uses the chat model

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    vaults_dir: Path = Path("vaults")
    default_vault: str = "default"
    vault_trash_days: int = 0          # delete trashed vaults (vaults/.trash) older than this; 0 = keep

    # Auth: the first admin is created from these on startup when no admin exists yet.
    admin_username: str = ""
    admin_password: str = ""
    data_dir: Path = Path("data")      # holds phosphochat.db (users, sessions, chat history)
    session_days: int = 14
    cookie_secure: bool = False        # set true when served over https

    topic_guard: bool = True           # only kinases, substrates, proteins, drugs, diseases
    guard_source_chars: int = 4000     # start of a source the topic check reads
    max_upload_mb: int = 60            # largest file accepted by capture
    model_timeout_seconds: int = 300   # per model request; a stalled request is retried
    capture_part_chars: int = 9000     # code reads every source in parts of this size
    # Reading notes handed to the writer, per job. 14000 kept every key fact the reader found on
    # the pilot papers (7000 dropped up to half). A model with a small per-request cap (Groq free
    # tier) gets a 413, and the writer retries with notes halved.
    capture_notes_chars: int = 14000
    # A job agent's history after its task, re-sent with every request. 40000 keeps the 3-4 pages
    # a capture updates (8-9k each on the pilot) whole; at 14000 writers re-read pages until the
    # request limit. Lower it for a model with a small per-request cap (Groq free tier: 14000).
    agent_history_chars: int = 40000
    # Sections the reader skips (a regex matched against `## ` headings, case-insensitive).
    capture_skip_sections: str = (
        r"references?|bibliography|acknowledge?ments?|author contributions?|contributions|"
        r"(declaration of )?(competing|conflicts? of) interests?|declarations?|funding|"
        r"data (and code )?availability|supplement(al|ary)( (information|materials?|data|figures|tables))?|"
        r"(online |materials and |star\W?)?methods|experimental procedures|key resources table")
    tool_output_chars: int = 8000
    verify_captures: bool = False       # fact-check capture/sync pages against their sources
    verify_source_chars: int = 12000    # source text sent to the fact-checker
    chat_keep_turns: int = 6            # recent turns sent to the model as they were
    chat_summarize: bool = True         # fold older turns into a running summary (else drop them)
    chat_summary_chars: int = 1500      # size cap of that summary
    chat_retention_days: int = 0        # delete conversations idle this long at startup; 0 = keep
    chat_prefetch: bool = True          # look up wiki + graph for the question before the model runs
    chat_context_chars: int = 6000      # size cap of that retrieved context
    chat_request_limit: int = 15
    job_request_limit: int = 40

    @property
    def vaults_path(self) -> Path:
        p = self.vaults_dir
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def db_path(self) -> Path:
        p = self.data_dir if self.data_dir.is_absolute() else REPO_ROOT / self.data_dir
        return p / "phosphochat.db"


_settings: Settings | None = None
_env_mtime: float | None = None
_env_keys: set[str] = set()


def _load_env_file() -> None:
    """Copy .env into os.environ; a key removed from .env falls back to its original value."""
    values = {k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None} \
        if ENV_FILE.is_file() else {}
    for k in _env_keys - values.keys():
        if k in _ORIGINAL_ENV:
            os.environ[k] = _ORIGINAL_ENV[k]
        else:
            os.environ.pop(k, None)
    _env_keys.clear()
    for k, v in values.items():
        os.environ[k] = v
        _env_keys.add(k)


_SECRET_FILE_RE = re.compile(r"^[A-Z][A-Z0-9_]*_FILE$")


def _load_secret_files() -> None:
    """Docker/Kubernetes secrets: `<NAME>_FILE=/run/secrets/x` sets NAME from that file, unless
    NAME is already set. Works for any setting (GROQ_API_KEY_FILE, ADMIN_PASSWORD_FILE, ...)
    and keeps the secret itself out of the container's environment listing."""
    for key, path in list(os.environ.items()):
        if not _SECRET_FILE_RE.match(key) or not path:
            continue
        name = key.removesuffix("_FILE")
        if os.environ.get(name):
            continue
        try:
            os.environ[name] = Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RuntimeError(f"{key} points to {path}, which can't be read: {e}") from e


def get_settings() -> Settings:
    global _settings, _env_mtime
    mtime = ENV_FILE.stat().st_mtime if ENV_FILE.is_file() else None
    if _settings is None or mtime != _env_mtime:
        _load_env_file()
        _load_secret_files()
        _settings = Settings()
        _env_mtime = mtime
    return _settings


get_settings()  # load .env at import so provider SDKs see their keys
