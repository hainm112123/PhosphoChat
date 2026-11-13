from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_vault, registry
from app.auth.deps import current_user, require_admin
from app.auth.store import User
from app.chat_store import chat_store
from app.config import get_settings
from app.graph import signor
from app.jobs.runner import jobs
from app.knowledge import claims, entities
from app.llm import check_role_models, role_models
from app.vault.store import VaultError

log = logging.getLogger("phosphochat")

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
async def health(user: User = Depends(current_user)) -> dict:
    try:
        overview = await asyncio.to_thread(signor.graph_overview)
        graph = {"ok": True, "relationships": overview["relationships"]}
    except RuntimeError as e:
        graph = {"ok": False, "error": str(e)}
    out = {"ok": True, "graph": graph, "models": role_models()}
    if user.is_admin:  # configuration details (which keys are missing) are for admins
        out["models_status"] = check_role_models()
    return out


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


# --------------------------------------------------------------------------- deleting

@router.get("/vaults/{name}/usage", dependencies=[Depends(require_admin)])
def vault_usage(name: str) -> dict:
    """What deleting this vault would remove (for the confirmation dialog)."""
    v = get_vault(name)
    return {"name": name, "pages": len(v.page_names()), "sources": len(v.list_sources()),
            "claims": len(v.read_relations()), "conversations": chat_store().count_vault(name),
            "active_jobs": len(jobs.active_for(name)), "is_default": name == get_settings().default_vault}


@router.delete("/vaults/{name}")
async def delete_vault(name: str, confirm: str = "", user: User = Depends(require_admin)) -> dict:
    """Move the vault folder to vaults/.trash and remove what lives outside it: its CLAIMED
    edges in the graph, every user's conversations in it, its finished jobs and caches."""
    if confirm != name:
        raise HTTPException(400, "Type the vault's name to confirm.")
    v = get_vault(name)
    if jobs.active_for(name):
        raise HTTPException(409, "A job is running on this vault; wait for it to finish.")
    async with v.lock:  # no job can start while the folder moves
        try:
            trashed = registry().trash(name, deleted_by=user.username)
        except OSError as e:
            raise HTTPException(500, f"Couldn't move the vault to the trash: {e}") from e
    try:
        claims_removed = await asyncio.to_thread(claims.remove_claims, name)
    except Exception:  # noqa: BLE001 - Neo4j down: the startup sweep removes them later
        claims_removed = None
    conversations_removed = chat_store().delete_vault(name)
    jobs.forget_vault(name)
    entities.forget(v)
    recreated = name == get_settings().default_vault
    if recreated:  # the app always has its default vault
        registry().ensure(name)
    log.warning("Vault '%s' deleted by %s (moved to %s; %s claim edges, %d conversations removed).",
                name, user.username, trashed, "?" if claims_removed is None else claims_removed,
                conversations_removed)
    return {"name": name, "trashed_to": str(trashed), "claims_removed": claims_removed, "conversations_removed": conversations_removed,
            "recreated": recreated}
