"""Networks for visualisation: the explorer page and the graphs under chat answers.

Both return the same shape, ready for Cytoscape:
    {"nodes": [{id, name, type, kinase_group, kinase_family, role, is_seed}],
     "edges": [{id, source, target, mechanism, sites, sources, n_sources, score, effect, signor_id, n_pmids}],
     "truncated": bool}
`role` is kinase | phosphatase | other, so the UI can colour nodes without a kinase group.
"""

from __future__ import annotations

from typing import Literal

from app.graph.signor import (
    ENTITY_TYPE, EDGE_FILTER, MAX_LIMIT, _filters, _resolve, _run, _site_label,
)

Direction = Literal["regulators", "targets", "both"]
GRAPH_TOOLS = {"regulators_of", "targets_of", "sites_of", "find_paths", "get_entity", "get_interaction"}


def _edge_rows(ids: list[str], direction: Direction, mechanism: str | None, filters: dict,
               limit: int) -> list[dict]:
    patterns = {"regulators": ["(a:Entity)-[m:MODIFIES]->(b:Entity) WHERE b.id IN $ids"],
                "targets": ["(a:Entity)-[m:MODIFIES]->(b:Entity) WHERE a.id IN $ids"]}
    patterns["both"] = patterns["regulators"] + patterns["targets"]
    union = "\nUNION\n".join(f"""
        MATCH {p} AND ($mechanism IS NULL OR m.mechanism = $mechanism) AND {EDGE_FILTER.format(r='m')}
        RETURN a.id AS source, b.id AS target, m.mechanism AS mechanism, m.sites AS sites,
               m.sources AS sources, m.n_sources AS n_sources, m.score AS score, m.effect AS effect,
               head(m.signor_ids) AS signor_id, size(m.pmids) AS n_pmids""" for p in patterns[direction])
    rows = _run(f"CALL {{ {union} }} RETURN * ORDER BY coalesce(score, 0) DESC, n_sources DESC LIMIT $limit",
                ids=ids, mechanism=mechanism, limit=limit, **filters)
    return rows


def _nodes(ids: list[str], seeds: set[str]) -> list[dict]:
    rows = _run(f"""
        MATCH (n:Entity) WHERE n.id IN $ids
        RETURN n.id AS id, n.name AS name, {ENTITY_TYPE.format(v='n')} AS type,
               n.kinase_group AS kinase_group, n.kinase_family AS kinase_family,
               EXISTS {{ (n)-[:PHOSPHORYLATES]->() }} AS phosphorylates,
               EXISTS {{ (n)-[:DEPHOSPHORYLATES]->() }} AS dephosphorylates""", ids=ids)
    out = []
    for r in rows:
        role = ("kinase" if r["kinase_group"] or r.pop("phosphorylates") else
                "phosphatase" if r["dephosphorylates"] else "other")
        r.pop("phosphorylates", None)
        r.pop("dephosphorylates")
        out.append({**r, "role": role, "is_seed": r["id"] in seeds})
    return out


def _finish(edge_rows: list[dict], seeds: set[str], truncated: bool) -> dict:
    edges, seen = [], set()
    for e in edge_rows:
        key = (e["source"], e["target"], e["mechanism"])
        if key in seen:
            continue
        seen.add(key)
        edges.append({**e, "id": "|".join(key), "sites": sorted({_site_label(s) for s in e["sites"] or []})})
    ids = sorted(seeds | {e["source"] for e in edges} | {e["target"] for e in edges})
    return {"nodes": _nodes(ids, seeds) if ids else [], "edges": edges, "truncated": truncated}


def network(seeds: list[str], depth: int = 1, direction: Direction = "both",
            sources: list[str] | None = None, min_score: float = 0, min_sources: int = 1,
            mechanism: str | None = None, limit: int = 300) -> dict:
    """The kinase-substrate neighbourhood of one or more proteins (depth 1 or 2)."""
    if not seeds:
        raise ValueError("Give at least one protein.")
    seed_ids: set[str] = set()
    for s in seeds[:10]:
        seed_ids |= {e["id"] for e in _resolve(s)}
    limit = max(10, min(int(limit), 1000))
    filters = _filters(sources, min_score, min_sources)
    rows = _edge_rows(sorted(seed_ids), direction, mechanism, filters, limit + 1)
    truncated = len(rows) > limit
    rows = rows[:limit]
    if depth >= 2 and not truncated:
        frontier = sorted({r["source"] for r in rows} | {r["target"] for r in rows} - seed_ids)
        if frontier:
            more = _edge_rows(frontier, direction, mechanism, filters, limit - len(rows) + 1)
            truncated = len(more) > limit - len(rows)
            rows += more[:max(0, limit - len(rows))]
    return _finish(rows, seed_ids, truncated)


# --------------------------------------------------------------------------- from tool results

def from_tool_result(tool: str, data: dict) -> dict | None:
    """Network for the graph under a chat answer, built from a graph tool's (parsed) result.
    Returns None when the result has nothing to draw."""
    if not isinstance(data, dict) or "error" in data:
        return None
    ids = data.get("matched_ids") or []
    rows: list[dict] = []

    def add(src, tgt, item):
        if src and tgt:
            rows.append({"source": src, "target": tgt, "mechanism": item.get("mechanism", "phosphorylation"),
                         "sites": [f"x_ph{s}" for s in item.get("sites", [])],
                         "sources": item.get("sources") or [], "n_sources": item.get("n_sources") or len(item.get("sources") or []),
                         "score": item.get("score"), "effect": item.get("effect"),
                         "signor_id": item.get("signor_id"), "n_pmids": len(item.get("pmids") or [])})

    centre = ids[0] if ids else None
    if tool == "regulators_of":
        for it in data.get("results", []):
            add(it.get("enzyme_id"), it.get("isoform") or centre, it)
    elif tool == "targets_of":
        for it in data.get("results", []):
            add(it.get("isoform") or centre, it.get("target_id"), it)
    elif tool == "get_entity":
        for it in data.get("top_regulators", []):
            add(it.get("enzyme_id"), it.get("isoform") or centre, it)
        for it in data.get("top_targets", []):
            add(it.get("isoform") or centre, it.get("target_id"), it)
    elif tool == "sites_of":
        for site in data.get("sites", []):
            for m in site.get("modified_by", []):
                add(m.get("enzyme_id"), site.get("protein_id") or centre,
                    {**m, "sites": [site.get("site")], "mechanism": m.get("mechanism")})
    elif tool == "find_paths":
        for path in data.get("paths", []):
            for h in path.get("hops", []):
                add(h.get("enzyme_id"), h.get("target_id"), h)
    else:
        return None
    if not rows:
        return None
    seeds = set(ids)
    if tool == "find_paths" and rows:
        seeds = {rows[0]["source"], rows[-1]["target"]}
    return _finish(rows[:MAX_LIMIT], seeds, truncated=False)
