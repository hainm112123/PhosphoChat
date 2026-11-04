"""Read-only browsing of a vault: pages, sources, index, log, git history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import get_vault
from typing import Literal

from pydantic import BaseModel

from app.auth.deps import current_user, require_admin
from app.auth.store import User
from app.vault.store import TABLE_EXTS, TEXT_EXTS, VaultError, split_frontmatter, _plain

# Any logged-in user may read every vault.
router = APIRouter(prefix="/api/vaults/{vault}", dependencies=[Depends(current_user)])


@router.get("/pages")
def pages(vault: str) -> list[dict]:
    return get_vault(vault).list_pages()


@router.get("/pages/{name}")
def page(vault: str, name: str) -> dict:
    v = get_vault(vault)
    try:
        raw = v.read_page(name)
        meta, body = split_frontmatter(raw)
    except VaultError as e:
        raise HTTPException(404, str(e)) from e
    return {"name": v.page_name(name), "meta": _plain(meta), "body": body,
            "backlinks": v.backlinks(name)}


@router.get("/sources")
def sources(vault: str) -> list[dict]:
    return get_vault(vault).list_sources()


@router.get("/sources/{name}")
def source(vault: str, name: str):
    v = get_vault(vault)
    try:
        path = v.source_path(name)
    except VaultError as e:
        raise HTTPException(400, str(e)) from e
    if not path.is_file():
        raise HTTPException(404, f"No source '{name}'.")
    if path.suffix.lower() not in TEXT_EXTS | TABLE_EXTS:
        return FileResponse(path, filename=path.name)
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = split_frontmatter(text) if path.suffix == ".md" else ({}, text)
    return {"name": path.name, "meta": _plain(meta), "body": body,
            "cited_by": [p["name"] for p in v.list_pages() if path.name in p["sources"]]}


@router.get("/index")
def index(vault: str) -> dict:
    v = get_vault(vault)
    return {"text": v.read_index(), "entries": v.index_entries()}


@router.get("/log")
def log(vault: str) -> dict:
    v = get_vault(vault)
    return {"text": v.read_log_text(), "entries": [e.as_dict() for e in v.log_entries()]}


@router.get("/history")
def history(vault: str, limit: int = 30) -> list[dict]:
    return get_vault(vault).history(limit)


# --------------------------------------------------------------------------- vault <-> graph

@router.get("/entity-pages")
def entity_pages(vault: str, ids: str) -> dict:
    """Wiki pages that mention each graph protein (for the Graph explorer)."""
    from app.knowledge.entities import entity_index
    v = get_vault(vault)
    index = entity_index(v)
    meta = {p["name"]: p for p in v.list_pages()}
    out = {}
    for i in [x.strip() for x in ids.split(",") if x.strip()][:50]:
        out[i] = [{"page": n, "title": meta.get(n, {}).get("title", n), "summary": meta.get(n, {}).get("summary", "")}
                  for n in index.get(i, [])]
    return out


@router.get("/claims")
def claims(vault: str, protein: str | None = None, status: str | None = None) -> list[dict]:
    """Kinase-substrate claims recorded from this vault's sources, with their status vs the databases."""
    from app.knowledge.claims import list_claims
    return list_claims(get_vault(vault), protein, status, include_rejected=True)


class ClaimReview(BaseModel):
    review: Literal["unreviewed", "reviewed", "rejected"]


@router.patch("/claims/{claim_id}", dependencies=[Depends(require_admin)])
async def review_claim(vault: str, claim_id: str, body: ClaimReview, user: User = Depends(require_admin)) -> dict:
    """Admin: mark a claim reviewed or rejected (committed to the vault's git)."""
    from app.knowledge.claims import safe_sync, statuses
    v = get_vault(vault)
    async with v.lock:
        try:
            claim = v.update_relation(claim_id, review=body.review, reviewed_by=user.username)
        except VaultError as e:
            raise HTTPException(404, str(e)) from e
        v.commit(f"claims: {body.review} {claim['enzyme']} -> {claim['target']}")
    safe_sync(v)
    return statuses([claim])[0]
