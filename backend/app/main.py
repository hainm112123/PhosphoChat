"""PhosphoChat API.

    cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.api import auth, chat, jobs, vaults, wiki  # noqa: E402
from app.api.deps import registry  # noqa: E402
from app.auth.deps import SameOriginMiddleware, auth_store  # noqa: E402
from app.config import REPO_ROOT, get_settings  # noqa: E402
from app.graph.signor import close_driver  # noqa: E402
from app.llm import role_models  # noqa: E402


log = logging.getLogger("phosphochat")


@asynccontextmanager
async def lifespan(_: FastAPI):
    reg = registry()
    reg.migrate_all()  # drop per-vault AGENTS.md/CLAUDE.md/skills left by older versions
    reg.ensure(get_settings().default_vault)  # empty vault from the template on first run
    s = get_settings()
    store = auth_store()
    if store.ensure_admin(s.admin_username, s.admin_password):
        log.warning("Created admin '%s' from ADMIN_USERNAME/ADMIN_PASSWORD.", s.admin_username)
    elif not store.has_admin():
        log.warning("No admin account yet: set ADMIN_USERNAME and ADMIN_PASSWORD in .env and restart.")
    _startup_report(store.has_admin())
    yield
    close_driver()


PROVIDER_KEYS = {"groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google-gla": "GOOGLE_API_KEY",
                 "openai": "OPENAI_API_KEY", "oxion": "OXION_API_KEY"}


def _startup_report(has_admin: bool) -> None:
    """One block in the logs showing what this deployment is configured with. Never prints
    secret values, only whether they are set."""
    lines = []
    for role, name in role_models().items():
        provider = name.split(":", 1)[0]
        env = PROVIDER_KEYS.get(provider)
        key = ("key set" if os.environ.get(env) else f"MISSING {env}") if env else "no key needed"
        lines.append(f"  {role:<6} {name}  ({key})")
    s = get_settings()
    lines += [f"  neo4j  {s.neo4j_uri}  (password {'set' if s.neo4j_password else 'MISSING'})",
              f"  admin  {'present' if has_admin else 'NONE: set ADMIN_USERNAME/ADMIN_PASSWORD'}",
              f"  vaults {s.vaults_path}   users db {s.db_path}"]
    print("PhosphoChat configuration:\n" + "\n".join(lines), flush=True)


app = FastAPI(title="PhosphoChat", lifespan=lifespan)
app.add_middleware(SameOriginMiddleware)
for r in (auth.router, vaults.router, wiki.router, jobs.router, chat.router):
    app.include_router(r)

# Serve the built frontend (npm run build) from the same origin when it exists;
# any non-API path falls back to index.html so client-side routes survive a reload.
_dist: Path = REPO_ROOT / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        f = (_dist / path).resolve()
        if path and f.is_file() and _dist.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(_dist / "index.html")
