from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import registry
from app.auth.deps import current_user, require_admin
from app.config import get_settings
from app.graph import signor
from app.llm import role_models
from app.vault.store import VaultError

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


class NewVault(BaseModel):
    name: str


@router.get("/config")
def config() -> dict:
    s = get_settings()
    models = role_models()
    return {"model": models["chat"], "models": models, "default_vault": s.default_vault,
            "verify_captures": s.verify_captures}


@router.get("/health")
async def health() -> dict:
    try:
        overview = await asyncio.to_thread(signor.graph_overview)
        graph = {"ok": True, "relationships": overview["relationships"]}
    except RuntimeError as e:
        graph = {"ok": False, "error": str(e)}
    return {"ok": True, "graph": graph, "models": role_models()}


@router.get("/vaults")
def list_vaults() -> list[dict]:
    reg = registry()
    out = []
    for name in reg.names():
        v = reg.get(name)
        out.append({"name": name, "pages": len(v.page_names()), "sources": len(v.list_sources())})
    return out


@router.post("/vaults", status_code=201, dependencies=[Depends(require_admin)])
def create_vault(body: NewVault) -> dict:
    try:
        v = registry().create(body.name.strip())
    except VaultError as e:
        raise HTTPException(400, str(e)) from e
    return {"name": v.name}
