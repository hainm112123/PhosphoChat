from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import registry
from app.config import get_settings
from app.graph import signor
from app.vault.store import VaultError

router = APIRouter(prefix="/api")


class NewVault(BaseModel):
    name: str


@router.get("/config")
def config() -> dict:
    s = get_settings()
    return {"model": s.model, "default_vault": s.default_vault, "verify_captures": s.verify_captures}


@router.get("/health")
async def health() -> dict:
    try:
        overview = await asyncio.to_thread(signor.graph_overview)
        graph = {"ok": True, "relationships": overview["relationships"]}
    except RuntimeError as e:
        graph = {"ok": False, "error": str(e)}
    return {"ok": True, "graph": graph, "model": get_settings().model}


@router.get("/vaults")
def list_vaults() -> list[dict]:
    reg = registry()
    out = []
    for name in reg.names():
        v = reg.get(name)
        out.append({"name": name, "pages": len(v.page_names()), "sources": len(v.list_sources())})
    return out


@router.post("/vaults", status_code=201)
def create_vault(body: NewVault) -> dict:
    try:
        v = registry().create(body.name.strip())
    except VaultError as e:
        raise HTTPException(400, str(e)) from e
    return {"name": v.name}
