"""PhosphoChat API.

    cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.api import chat, jobs, vaults, wiki  # noqa: E402
from app.api.deps import registry  # noqa: E402
from app.config import REPO_ROOT, get_settings  # noqa: E402
from app.graph.signor import close_driver  # noqa: E402


@asynccontextmanager
async def lifespan(_: FastAPI):
    reg = registry()
    reg.migrate_all()  # drop per-vault AGENTS.md/CLAUDE.md/skills left by older versions
    reg.ensure(get_settings().default_vault)  # empty vault from the template on first run
    yield
    close_driver()


app = FastAPI(title="PhosphoChat", lifespan=lifespan)
for r in (vaults.router, wiki.router, jobs.router, chat.router):
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
