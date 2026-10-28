"""PhosphoSIGNOR graph queries (ported from mcp/server/signor_server.py).

Same Cypher and behaviour as the signor-graph MCP server, but as plain functions that the
backend's agents call directly. Tools take plain biological names ("TP53", "ARX", "Q96QS3")
instead of Cypher. The graph is built by scripts/load_signor.py.

Errors: ValueError = bad input / no match (the model can fix its call);
RuntimeError = database unreachable or credentials rejected.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from neo4j import GraphDatabase, RoutingControl
from neo4j.exceptions import AuthError, ServiceUnavailable
from pydantic import Field

from app.config import get_settings

INSTRUCTIONS = """\
The graph tools answer questions about protein phosphorylation signalling, using the
PhosphoSIGNOR dataset (~13,400 curated records: a kinase or phosphatase modifies a
phosphosite on a target protein, which up- or down-regulates that protein).

Pick the tool that matches the question and pass plain names (gene symbols such as
"TP53", or UniProt IDs such as "P04637"):
- Unsure of the exact name/ID          -> search_entities
- Summary of one protein/complex       -> get_entity
- Who phosphorylates / regulates X     -> regulators_of
- What does kinase/phosphatase X act on -> targets_of
- Which phosphosites does X have       -> sites_of
- How does A connect to B              -> find_paths
- Details/papers behind a record       -> get_evidence
- What is in the database              -> graph_overview

Rules:
- Use these tools for every graph question. Never write scripts or Cypher; everything you
  need is exposed here.
- Cite graph facts with their signor_id (e.g. SIGNOR-277418) and PMIDs.
- 'score' is the SIGNOR confidence score (0-1, higher = better supported).
- 'effect' is the effect of the modification on the target protein's activity.
"""

MAX_LIMIT = 200
Mechanism = Literal["phosphorylation", "dephosphorylation"]

# --------------------------------------------------------------------------- database

_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        s = get_settings()
        _driver = GraphDatabase.driver(
            s.neo4j_uri,
            auth=(s.neo4j_username, s.neo4j_password),
            notifications_min_severity="OFF",
            # Fail fast when Neo4j is down: the agent gets "database unreachable" in seconds
            # instead of waiting out the driver's default 30 s of retries per tool call.
            max_transaction_retry_time=3,
            connection_timeout=5,
        )
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def _run(query: str, **params) -> list[dict[str, Any]]:
    s = get_settings()
    try:
        records, _, _ = _get_driver().execute_query(
            query, params, routing_=RoutingControl.READ, database_=s.neo4j_database,
        )
    except ServiceUnavailable as e:
        raise RuntimeError(f"Cannot reach the Neo4j database. Is it running? ({s.neo4j_uri})") from e
    except AuthError as e:
        raise RuntimeError("Neo4j rejected the credentials; check NEO4J_PASSWORD.") from e
    return [r.data() for r in records]


# --------------------------------------------------------------------------- helpers

ENTITY_TYPE = "[l IN labels({v}) WHERE l <> 'Entity'][0]"


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _site_label(site_id: str) -> str:
    """'P04637_phSer15' -> 'Ser15'."""
    return site_id.split("_ph", 1)[-1]


def _resolve(name_or_id: str) -> list[dict]:
    """Resolve a gene symbol / UniProt ID / SIGNOR ID to entity nodes.

    An exact ID wins; otherwise every entity with that name is returned (some gene
    symbols cover several isoforms, e.g. MAPT).
    """
    q = name_or_id.strip()
    if not q:
        raise ValueError("Empty name. Pass a gene symbol (e.g. 'TP53') or UniProt ID.")
    if "_ph" in q:
        raise ValueError(f"'{q}' is a phosphosite, not a protein. Pass the protein "
                         f"('{q.split('_ph')[0]}') and use sites_of to list its sites.")
    rows = _run(f"""
        MATCH (n:Entity)
        WHERE toUpper(n.id) = toUpper($q) OR toUpper(n.name) = toUpper($q)
        RETURN n.id AS id, n.name AS name, {ENTITY_TYPE.format(v='n')} AS type,
               toUpper(n.id) = toUpper($q) AS id_match
        ORDER BY n.id""", q=q)
    exact_id = [r for r in rows if r["id_match"]]
    rows = exact_id or rows
    if not rows:
        hints = _search(q, 8)
        hint = ", ".join(f"{h['name']} ({h['id']})" for h in hints) or "none"
        raise ValueError(f"No protein/complex named '{q}'. Did you mean: {hint}? "
                         "Use search_entities to look names up.")
    return [{k: r[k] for k in ("id", "name", "type")} for r in rows]


def _search(query: str, limit: int) -> list[dict]:
    return _run(f"""
        MATCH (n:Entity)
        WHERE toUpper(n.name) CONTAINS toUpper($q) OR toUpper(n.id) CONTAINS toUpper($q)
        WITH n, CASE
                  WHEN toUpper(n.name) = toUpper($q) OR toUpper(n.id) = toUpper($q) THEN 0
                  WHEN toUpper(n.name) STARTS WITH toUpper($q) THEN 1
                  ELSE 2 END AS rank
        ORDER BY rank, size(n.name), n.name
        LIMIT $limit
        RETURN n.id AS id, n.name AS name, {ENTITY_TYPE.format(v='n')} AS type,
               COUNT {{ (n)<-[:MODIFIES]-() }} AS n_regulator_records,
               COUNT {{ (n)-[:MODIFIES]->() }} AS n_target_records,
               COUNT {{ (n)<-[:SITE_OF]-() }} AS n_sites""", q=query.strip(), limit=limit)


def _edges(direction: Literal["in", "out"], ids: list[str], mechanism: str | None,
           min_score: float) -> list[dict]:
    """MODIFIES edges into (regulators) or out of (targets) the given entities."""
    pattern = "(other:Entity)-[m:MODIFIES]->(n:Entity)" if direction == "in" \
        else "(n:Entity)-[m:MODIFIES]->(other:Entity)"
    rows = _run(f"""
        MATCH {pattern}
        WHERE n.id IN $ids AND m.score >= $min_score
          AND ($mechanism IS NULL OR m.mechanism = $mechanism)
        RETURN other.name AS name, other.id AS id, {ENTITY_TYPE.format(v='other')} AS type,
               n.id AS via_id, m.mechanism AS mechanism, m.sites AS sites,
               m.target_effect AS effect, m.score AS score, m.signor_id AS signor_id,
               m.pmids AS pmids
        ORDER BY m.score DESC, other.name""",
                ids=ids, mechanism=mechanism, min_score=min_score)
    for r in rows:
        r["sites"] = [_site_label(s) for s in r["sites"]]
    return rows


def _shape(rows: list[dict], role: str, ids: list[str], limit: int) -> list[dict]:
    """Rename generic keys to enzyme_*/target_* and drop via_id when unambiguous."""
    out = []
    for r in rows[:limit]:
        item = {f"{role}": r["name"], f"{role}_id": r["id"], f"{role}_type": r["type"],
                "mechanism": r["mechanism"], "sites": r["sites"], "effect": r["effect"],
                "score": r["score"], "signor_id": r["signor_id"], "pmids": r["pmids"]}
        if len(ids) > 1:
            item["isoform"] = r["via_id"]
        out.append(item)
    return out


def _sites(ids: list[str]) -> list[dict]:
    rows = _run("""
        MATCH (s:Site)-[:SITE_OF]->(p:Entity) WHERE p.id IN $ids
        OPTIONAL MATCH (e:Entity)-[r:PHOSPHORYLATES|DEPHOSPHORYLATES]->(s)
        OPTIONAL MATCH (s)-[g:REGULATES]->(p) WHERE g.signor_id = r.signor_id
        WITH s, p, e, r, g ORDER BY r.score DESC
        RETURN s.id AS site_id, s.residue AS residue, s.position AS position,
               p.id AS protein_id,
               collect(CASE WHEN e IS NULL THEN NULL ELSE {
                   enzyme: e.name, enzyme_id: e.id,
                   mechanism: CASE type(r) WHEN 'PHOSPHORYLATES' THEN 'phosphorylation'
                                           ELSE 'dephosphorylation' END,
                   effect: g.effect, score: r.score, signor_id: r.signor_id} END) AS modified_by
        ORDER BY s.position, s.residue""", ids=ids)
    for r in rows:
        r["site"] = f"{r.pop('residue')}{r.pop('position')}"
        if len(ids) == 1:
            del r["protein_id"]
    return rows


# --------------------------------------------------------------------------- tools

Name = Annotated[str, Field(description="Gene symbol (e.g. 'TP53'), UniProt ID (e.g. 'P04637') "
                                        "or SIGNOR ID (e.g. 'SIGNOR-C1'). Case-insensitive.")]
MinScore = Annotated[float, Field(ge=0, le=1, description="Only records with SIGNOR score >= this "
                                                          "(0-1). 0 = everything.")]
Limit = Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Maximum results to return.")]
MechanismArg = Annotated[Mechanism | None, Field(description="Restrict to 'phosphorylation' or "
                                                             "'dephosphorylation'. Omit for both.")]


def search_entities(
    query: Annotated[str, Field(description="Full or partial gene symbol, protein name or ID.")],
    limit: Limit = 10,
) -> dict:
    """Look up proteins, complexes and protein families by (partial) name or ID.

    Use this when you are unsure of the exact gene symbol. Returns matches with their
    type and how many regulator/target records and phosphosites each has.
    """
    rows = _search(query, _clamp(limit))
    return {"query": query, "results": rows}


def get_entity(name_or_id: Name) -> dict:
    """Profile of one protein/complex: identity, counts, top regulators, top targets and its phosphosites.

    Good first call for "tell me about X".
    """
    ents = _resolve(name_or_id)
    ids = [e["id"] for e in ents]
    regs = _edges("in", ids, None, 0)
    tgts = _edges("out", ids, None, 0)
    sites = _sites(ids)
    return {
        "entity": ents[0]["name"], "type": ents[0]["type"], "matched_ids": ids,
        "counts": {"regulator_records": len(regs), "target_records": len(tgts),
                   "phosphosites": len(sites)},
        "top_regulators": _shape(regs, "enzyme", ids, 5),
        "top_targets": _shape(tgts, "target", ids, 5),
        "phosphosites": sorted({s["site"] for s in sites},
                               key=lambda x: (int("".join(filter(str.isdigit, x)) or 0), x)),
    }


def regulators_of(target: Name, mechanism: MechanismArg = None, min_score: MinScore = 0,
                  limit: Limit = 50) -> dict:
    """Kinases/phosphatases that modify a protein: who (de)phosphorylates X, at which sites, with what effect.

    Answers "which kinases phosphorylate X?" or "what regulates X?". Sorted by score.
    """
    ents = _resolve(target)
    ids = [e["id"] for e in ents]
    rows = _edges("in", ids, mechanism, min_score)
    return {"target": ents[0]["name"], "matched_ids": ids, "total": len(rows),
            "results": _shape(rows, "enzyme", ids, _clamp(limit))}


def targets_of(enzyme: Name, mechanism: MechanismArg = None, min_score: MinScore = 0,
               limit: Limit = 50) -> dict:
    """Substrates of a kinase or phosphatase: which proteins X (de)phosphorylates, at which sites, with what effect.

    Answers "what does kinase X phosphorylate?". Sorted by score.
    """
    ents = _resolve(enzyme)
    ids = [e["id"] for e in ents]
    rows = _edges("out", ids, mechanism, min_score)
    return {"enzyme": ents[0]["name"], "matched_ids": ids, "total": len(rows),
            "results": _shape(rows, "target", ids, _clamp(limit))}


def sites_of(protein: Name) -> dict:
    """Phosphosites on a protein, each with the enzymes that modify it and the effect on the protein.

    Answers "which residues of X are phosphorylated, and by whom?".
    """
    ents = _resolve(protein)
    ids = [e["id"] for e in ents]
    rows = _sites(ids)
    return {"protein": ents[0]["name"], "matched_ids": ids, "total_sites": len(rows),
            "sites": rows}


def find_paths(
    source: Name,
    target: Name,
    max_hops: Annotated[int, Field(ge=1, le=4, description="Maximum path length (1-4).")] = 3,
    min_score: MinScore = 0,
    limit: Annotated[int, Field(ge=1, le=50, description="Maximum paths to return.")] = 5,
) -> dict:
    """Shortest signalling paths from source to target, following enzyme -> substrate modifications.

    Each hop says which enzyme modified which protein, at which sites, with what effect.
    Answers "how does A affect B?" / "is there a pathway from A to B?". Paths are ranked
    by their weakest hop's score.
    """
    src, dst = _resolve(source), _resolve(target)
    hops = max(1, min(int(max_hops), 4))  # interpolated into the Cypher: keep the clamp
    rows = _run(f"""
        MATCH (a:Entity) WHERE a.id IN $src
        MATCH (b:Entity) WHERE b.id IN $dst AND a <> b
        MATCH p = allShortestPaths((a)-[:MODIFIES*1..{hops}]->(b))
        WHERE all(r IN relationships(p) WHERE r.score >= $min_score)
        WITH p, reduce(m = 1.0, r IN relationships(p) | CASE WHEN r.score < m THEN r.score ELSE m END)
                AS weakest
        ORDER BY weakest DESC
        LIMIT $limit
        RETURN weakest AS weakest_score, [r IN relationships(p) | {{
            enzyme: startNode(r).name, target: endNode(r).name, mechanism: r.mechanism,
            sites: r.sites, effect: r.target_effect, score: r.score, signor_id: r.signor_id
        }}] AS hops""",
                src=[e["id"] for e in src], dst=[e["id"] for e in dst],
                min_score=min_score, limit=max(1, min(int(limit), 50)))
    for r in rows:
        for h in r["hops"]:
            h["sites"] = [_site_label(s) for s in h["sites"]]
        r["route"] = " -> ".join([r["hops"][0]["enzyme"]] + [h["target"] for h in r["hops"]])
    result = {"source": src[0]["name"], "target": dst[0]["name"], "paths": rows}
    if not rows:
        result["note"] = (f"No path within {hops} hops"
                          + (f" with score >= {min_score}" if min_score else "")
                          + ". Try a larger max_hops (up to 4) or a lower min_score.")
    return result


def get_evidence(
    signor_id: Annotated[str, Field(description="SIGNOR record ID, e.g. 'SIGNOR-277418' "
                                                "(the number alone also works).")],
) -> dict:
    """Full detail of one SIGNOR record: enzyme, phosphosite(s), target, effects, score and PubMed links."""
    sid = signor_id.strip().upper()
    if not sid.startswith("SIGNOR-"):
        sid = f"SIGNOR-{sid}"
    rows = _run("""
        MATCH (a)-[r]->(b) WHERE r.signor_id = $sid AND type(r) <> 'MODIFIES'
        RETURN type(r) AS relation, a.name AS source, b.name AS target,
               r.effect AS effect, r.score AS score, r.pmids AS pmids
        ORDER BY CASE type(r) WHEN 'REGULATES' THEN 1 ELSE 0 END, b.name""", sid=sid)
    if not rows:
        raise ValueError(f"No record with signor_id '{sid}'.")
    score = rows[0]["score"]
    pmids = sorted({p for r in rows for p in r.pop("pmids")})
    for r in rows:
        del r["score"]
    return {"signor_id": sid, "score": score, "steps": rows, "pmids": pmids,
            "pubmed_urls": [f"https://pubmed.ncbi.nlm.nih.gov/{p}/" for p in pmids],
            "signor_url": f"https://signor.uniroma2.it/relation_result.php?id={sid}"}


def graph_overview() -> dict:
    """What the database contains: node/edge counts and how the data is modelled."""
    nodes = _run("MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label")
    rels = _run("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY type")
    return {
        "source": "PhosphoSIGNOR (SIGNOR database, phosphorylation/dephosphorylation subset)",
        "nodes": {r["label"]: r["count"] for r in nodes},
        "relationships": {r["type"]: r["count"] for r in rels},
        "model": [
            "Entity = Protein | Complex | ProteinFamily | FusionProtein | Phenotype (id, name)",
            "Site = a phosphorylatable residue on a protein, e.g. TP53 Ser15",
            "enzyme -PHOSPHORYLATES/DEPHOSPHORYLATES-> Site -REGULATES-> protein",
            "enzyme -MODIFIES-> protein is the shortcut used by regulators_of/targets_of/find_paths",
        ],
        "mechanisms": ["phosphorylation", "dephosphorylation"],
        "effects": ["up-regulates", "down-regulates"],
    }


GRAPH_TOOLS = [search_entities, get_entity, regulators_of, targets_of, sites_of, find_paths,
               get_evidence, graph_overview]
