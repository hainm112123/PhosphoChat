"""Shared helpers for the evaluation scripts: paths, JSON results, the in-process app client."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
logging.getLogger("neo4j").setLevel(logging.ERROR)
logging.getLogger("pymupdf").setLevel(logging.ERROR)

EVAL_DIR = Path(__file__).resolve().parent
RESULTS = EVAL_DIR / "results"
# Readings are kept on disk, so an iteration on the writer or chat doesn't read the papers again.
os.environ.setdefault("READING_CACHE_DIR", str(RESULTS / "readings"))
DATASET = Path(os.environ.get("EVAL_DATASET", r"C:\yuuhi\int3132\dataset\pdf"))

ADMIN = ("evaladmin", "eval-pass-123")


def run_dir(name: str) -> Path:
    d = RESULTS / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.split(":", 1)[-1].lower()).strip("-")


@contextmanager
def app_client():
    """A TestClient on the real app, logged in as an admin of a throwaway user database.
    Vaults are the real ones (VAULTS_DIR), so evaluation vaults can be opened in the UI."""
    from fastapi.testclient import TestClient

    from app.auth import deps
    from app.auth.store import AuthStore
    store = AuthStore(Path(tempfile.mkdtemp()) / "auth.db")
    deps.set_store(store)
    store.create_user(ADMIN[0], ADMIN[1], "admin")
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]})
        assert r.status_code == 200, r.text
        yield client


def sse_events(raw: str) -> list[tuple[str, dict]]:
    out = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            try:
                out.append((lines["event"], json.loads(lines["data"])))
            except (KeyError, json.JSONDecodeError):
                pass
    return out


def use_groq_key(which: str) -> None:
    """`--groq-key alternative`: run on the second free Groq key (GROQ_API_KEY_ALTERNATIVE in .env),
    so testing doesn't spend the main key's daily quota."""
    if which == "main":
        return
    from app import llm
    from app.config import get_settings
    get_settings()  # loads .env into os.environ
    alt = os.environ.get("GROQ_API_KEY_ALTERNATIVE")
    if not alt:
        raise SystemExit("GROQ_API_KEY_ALTERNATIVE is not set in .env")
    os.environ["GROQ_API_KEY"] = alt
    llm._build.cache_clear()


def add_groq_key_arg(ap) -> None:
    ap.add_argument("--groq-key", choices=["main", "alternative"], default="main")


def set_role_model(role: str, model: str) -> None:
    """Point one model role at `model` for this process (the .env value is untouched)."""
    from app.config import get_settings
    setattr(get_settings(), f"{role}_model", model)


# What decides the app's behaviour: a run records a hash of these files, so two runs with the
# same hash ran the same prompts and code.
DESIGN_FILES = ["app/prompts", "app/agents", "app/tools/agent_tools.py", "app/graph/agent_tools.py",
                "app/knowledge/context.py"]


def design_hash() -> str:
    import hashlib
    root = EVAL_DIR.parent
    h = hashlib.sha256()
    for entry in DESIGN_FILES:
        p = root / entry
        files = sorted(p.rglob("*")) if p.is_dir() else [p]
        for f in files:
            if f.is_file() and f.suffix in (".py", ".md"):
                h.update(str(f.relative_to(root)).encode())
                h.update(f.read_bytes())
    return h.hexdigest()[:12]


def record_run(kind: str, **fields) -> str:
    """Append one line to results/runs.jsonl (the run ledger); returns the run id."""
    from datetime import datetime

    from app.config import get_settings
    from app.llm import role_models
    s = get_settings()
    run_id = f"{kind}-{datetime.now():%Y%m%d-%H%M%S}"
    row = {"run": run_id, "kind": kind, "design": design_hash(), "models": role_models(),
           "settings": {k: getattr(s, k) for k in ("capture_part_chars", "capture_notes_chars",
                                                   "chat_context_chars", "verify_captures")},
           **fields}
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return run_id
