"""Graph explorer API: search, neighbourhood networks, interaction detail. Any logged-in user."""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import current_user
from app.graph import network as net
from app.graph import signor

router = APIRouter(prefix="/api/graph", dependencies=[Depends(current_user)])

GROUP_ORDER = ["TK", "TKL", "STE", "CK1", "AGC", "CAMK", "CMGC", "Atypical", "Other", "RGC"]


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e


@router.get("/search")
async def search(q: str = Query(min_length=1), limit: int = 10) -> list[dict]:
    return (await asyncio.to_thread(_call, signor.search_entities, q, max(1, min(limit, 25))))["results"]


@router.get("/network")
async def network(seeds: str, depth: int = 1, direction: net.Direction = "both",
                  sources: str | None = None, min_score: float = 0, min_sources: int = 1,
                  mechanism: str | None = None, limit: int = 300) -> dict:
    seed_list = [s.strip() for s in seeds.split(",") if s.strip()]
    src = [s.strip() for s in sources.split(",") if s.strip()] if sources else None
    return await asyncio.to_thread(_call, net.network, seed_list, max(1, min(depth, 2)), direction,
                                   src, min_score, min_sources, mechanism or None, limit)


@router.get("/interaction")
async def interaction(enzyme: str, target: str) -> dict:
    return await asyncio.to_thread(_call, signor.get_interaction, enzyme, target)


@router.get("/meta")
async def meta() -> dict:
    overview = await asyncio.to_thread(_call, signor.graph_overview)
    groups_file = Path(__file__).resolve().parents[3] / "seed" / "kinase_groups.csv"
    counts: dict[str, int] = {}
    if groups_file.is_file():
        for r in csv.DictReader(groups_file.open(encoding="utf-8")):
            counts[r["group"]] = counts.get(r["group"], 0) + 1
    return {"kinase_groups": [g for g in GROUP_ORDER if counts.get(g)],
            "sources": list(signor.SOURCES), "links_per_source": overview["links_per_source"],
            "dataset": overview["dataset"]}
