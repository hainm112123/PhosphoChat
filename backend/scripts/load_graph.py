"""Load the merged kinase-substrate graph: PhosphoSIGNOR + KiNet (PhosphoSitePlus, EPSD, iPTMNet).

    python scripts/load_graph.py --wipe            # rebuild from seed/
    python scripts/load_graph.py --if-outdated     # rebuild only when seed/ changed (containers)
    python scripts/load_graph.py --dry-run         # parse, merge and print stats; no database

Graph model (one edge per interaction, carrying every database that reports it)
-----------------------------------------------------------------------------
    (:Entity:Protein|Complex|... {id, name, kinase_group?, kinase_family?})
        -[:PHOSPHORYLATES|DEPHOSPHORYLATES {sources[], n_sources, signor_ids[], pmids[],
                                            evidence[], score?, effect?, records_json}]->
    (:Site {id, name, residue, position, protein_id})
        -[:REGULATES {signor_id, mechanism, effect, pmids, score}]->(:Entity)   SIGNOR only
    (:Site)-[:SITE_OF]->(:Entity)
    (:Entity)-[:MODIFIES {mechanism, sites[], sources[], n_sources, signor_ids[], pmids[],
                          score?, effect?, records_json}]->(:Entity)              one per (enzyme, target, mechanism)
    (:Meta {dataset_version, loaded_at, counts_json})

`score`/`effect` come from SIGNOR (best-scored record) and are null for links only the KiNet
databases report. `records_json` keeps the per-source detail (database, evidence, PMIDs, SIGNOR
id/score/effect) as JSON, because Neo4j can't store lists of maps.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import load_signor as signor  # noqa: E402  (SIGNOR parsing, batching, connection helpers)

REPO = Path(__file__).resolve().parents[2]
SEED = REPO / "seed"
SIGNOR_FILE = signor.DEFAULT_FILE
KINET_FILE = SEED / "ksi_source_full_dataset.csv"
GROUPS_FILE = SEED / "kinase_groups.csv"
SCHEMA_VERSION = "merged-v1"   # bump when the graph model changes
KINET_SOURCES = ("PhosphoSitePlus", "EPSD", "iPTMNet")
RESIDUES = {"S": "Ser", "T": "Thr", "Y": "Tyr", "H": "His"}
KINET_SITE_RE = re.compile(r"^([STYH])(\d+)$")


# --------------------------------------------------------------------------- parsing

def _na(v: str | None) -> str:
    v = (v or "").strip()
    return "" if v.upper() == "NA" else v


def parse_kinet(path: Path):
    """KiNet rows -> (entities, sites, records). Blank separator rows and residues other than
    S/T/Y/H are skipped; each record is one (kinase, substrate, site, database) row."""
    entities, sites, records, skipped = {}, {}, [], Counter()
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            kinase, substrate = _na(row.get("Kinase")), _na(row.get("Substrate"))
            if not kinase or not substrate:
                skipped["blank"] += 1
                continue
            m = KINET_SITE_RE.match(_na(row.get("Site")))
            if not m:
                skipped["residue"] += 1
                continue
            res, pos = RESIDUES[m.group(1)], int(m.group(2))
            k_name = _na(row.get("Kinase Name")) or kinase
            s_name = _na(row.get("Substrate Name")) or substrate
            entities.setdefault(kinase, {"id": kinase, "name": k_name, "label": "Protein"})
            entities.setdefault(substrate, {"id": substrate, "name": s_name, "label": "Protein"})
            site_id = f"{substrate}_ph{res}{pos}"
            sites.setdefault(site_id, {"id": site_id, "name": f"{s_name}_ph{res}{pos}", "residue": res,
                                       "position": pos, "protein_id": substrate, "protein_name": s_name})
            records.append({
                "source": _na(row.get("Source Database")) or "KiNet",
                "enzyme": kinase, "target": substrate, "site": site_id,
                "mechanism": "phosphorylation",
                "evidence": _na(row.get("Evidence")),
                "pmids": [p.strip() for p in _na(row.get("Reference (PMID)")).split(";") if p.strip()],
            })
    return entities, sites, records, skipped


def parse_groups(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {r["uniprot"]: {"group": r["group"], "family": r["family"]} for r in csv.DictReader(fh)}


# --------------------------------------------------------------------------- merging

def _edge(records: list[dict], key: dict) -> dict:
    """One merged edge from its source records."""
    signor_recs = sorted((r for r in records if r["source"] == "SIGNOR"),
                         key=lambda r: -(r.get("score") or 0))
    best = signor_recs[0] if signor_recs else None
    sources = sorted({r["source"] for r in records}, key=lambda s: (s != "SIGNOR", s))
    pmids = sorted({p for r in records for p in r.get("pmids", [])}, key=lambda p: (len(p), p))
    evidence = sorted({e.strip() for r in records for e in (r.get("evidence") or "").split(";") if e.strip()})
    detail = [{k: r[k] for k in ("source", "signor_id", "score", "effect", "evidence", "pmids", "sites")
               if r.get(k) not in (None, "", [])} for r in records]
    return {**key, "sources": sources, "n_sources": len(sources),
            "signor_ids": [r["signor_id"] for r in signor_recs],
            "pmids": pmids, "evidence": evidence,
            "score": best["score"] if best else None,
            "effect": best.get("effect") if best else None,
            "records_json": json.dumps(detail, separators=(",", ":"))}


def merge(signor_parsed, kinet_parsed, groups: dict[str, dict]):
    """Combine SIGNOR and KiNet into entities, sites and merged edges."""
    s_entities, s_sites, s_enzyme, s_regulates, s_modifies, _warnings = signor_parsed
    k_entities, k_sites, k_records, _skipped = kinet_parsed

    entities = dict(k_entities)
    entities.update(s_entities)              # SIGNOR names/labels win when both have the entity
    sites = {**k_sites, **s_sites}
    for s in sites.values():                 # every site's protein exists
        entities.setdefault(s["protein_id"], {"id": s["protein_id"], "name": s["protein_name"],
                                              "label": "Protein"})
    for eid, e in entities.items():
        g = groups.get(eid)
        e["kinase_group"], e["kinase_family"] = (g["group"], g["family"]) if g else (None, None)

    # effect on the target protein, per SIGNOR record (from the site -> protein row)
    target_effect = {(m["signor_id"], m["enzyme"]): m.get("target_effect") for m in s_modifies}

    site_recs: dict[tuple, list] = defaultdict(list)
    for e in s_enzyme:
        mech = "phosphorylation" if e["rel"] == "PHOSPHORYLATES" else "dephosphorylation"
        site_recs[(e["enzyme"], e["site"], mech)].append({
            "source": "SIGNOR", "signor_id": e["signor_id"], "score": e["score"],
            "effect": target_effect.get((e["signor_id"], e["enzyme"])), "pmids": e["pmids"]})
    mod_recs: dict[tuple, list] = defaultdict(list)
    for m in s_modifies:
        mod_recs[(m["enzyme"], m["target"], m["mechanism"])].append({
            "source": "SIGNOR", "signor_id": m["signor_id"], "score": m["score"],
            "effect": m.get("target_effect"), "pmids": m["pmids"], "sites": m["sites"]})
    for r in k_records:
        rec = {"source": r["source"], "evidence": r["evidence"], "pmids": r["pmids"]}
        site_recs[(r["enzyme"], r["site"], r["mechanism"])].append(rec)
        mod_recs[(r["enzyme"], r["target"], r["mechanism"])].append({**rec, "sites": [r["site"]]})

    site_edges = [_edge(recs, {"enzyme": k[0], "site": k[1], "mechanism": k[2],
                               "rel": signor.ENZYME_RELS[k[2]]}) for k, recs in site_recs.items()]
    modifies = []
    for (enz, tgt, mech), recs in mod_recs.items():
        edge = _edge(recs, {"enzyme": enz, "target": tgt, "mechanism": mech})
        edge["sites"] = sorted({s for r in recs for s in r.get("sites", [])})
        modifies.append(edge)
    return entities, sites, site_edges, s_regulates, modifies


def dataset_version(*files: Path) -> str:
    h = hashlib.sha256(SCHEMA_VERSION.encode())
    for f in files:
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def stats(entities, sites, site_edges, modifies) -> dict:
    per_source = Counter(s for e in modifies for s in e["sources"])
    return {
        "entities": len(entities), "sites": len(sites),
        "site_edges": len(site_edges), "modifies": len(modifies),
        "modifies_per_source": dict(per_source),
        "modifies_multi_source": sum(1 for e in modifies if e["n_sources"] > 1),
        "kinases_with_group": sum(1 for e in entities.values() if e.get("kinase_group")),
    }


# --------------------------------------------------------------------------- loading

LOAD_ENTITIES = """
UNWIND $rows AS row
MERGE (n:Entity {id: row.id})
SET n:%s, n.name = row.name, n.kinase_group = row.kinase_group, n.kinase_family = row.kinase_family
"""
LOAD_SITE_EDGES = """
UNWIND $rows AS row
MATCH (e:Entity {id: row.enzyme})
MATCH (s:Site {id: row.site})
MERGE (e)-[r:%s]->(s)
SET r.sources = row.sources, r.n_sources = row.n_sources, r.signor_ids = row.signor_ids,
    r.pmids = row.pmids, r.evidence = row.evidence, r.score = row.score, r.effect = row.effect,
    r.records_json = row.records_json
"""
LOAD_MODIFIES = """
UNWIND $rows AS row
MATCH (e:Entity {id: row.enzyme})
MATCH (t:Entity {id: row.target})
MERGE (e)-[r:MODIFIES {mechanism: row.mechanism}]->(t)
SET r.sites = row.sites, r.sources = row.sources, r.n_sources = row.n_sources,
    r.signor_ids = row.signor_ids, r.pmids = row.pmids, r.evidence = row.evidence,
    r.score = row.score, r.effect = row.effect, r.records_json = row.records_json
"""


def current_version(driver, db: str) -> str | None:
    records, _, _ = driver.execute_query("MATCH (m:Meta) RETURN m.dataset_version AS v LIMIT 1",
                                         database_=db)
    return records[0]["v"] if records else None


def node_count(driver, db: str) -> int:
    records, _, _ = driver.execute_query("MATCH (n) RETURN count(n) AS n", database_=db)
    return records[0]["n"]


def load(args, version, entities, sites, site_edges, regulates, modifies, counts):
    from neo4j import GraphDatabase
    with GraphDatabase.driver(args.uri, auth=(args.user, args.password)) as driver:
        signor._wait_for(driver, args.wait)
        db = args.database
        if args.if_outdated and current_version(driver, db) == version:
            print(f"Graph already up to date (dataset {version}), skipping.")
            return
        if args.if_empty and node_count(driver, db) > 0:
            print("Graph already loaded, skipping.")
            return
        if args.wipe or args.if_outdated:
            print("Wiping existing graph ...")
            with driver.session(database=db) as session:
                session.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
        for stmt in signor.SCHEMA:
            driver.execute_query(stmt, database_=db)

        print("Loading ...")
        by_label = defaultdict(list)
        for e in entities.values():
            by_label[e["label"]].append(e)
        for label in signor.ENTITY_LABELS:  # whitelist: labels are interpolated into Cypher
            if by_label[label]:
                signor.run_batches(driver, db, LOAD_ENTITIES % label, by_label[label], args.batch_size,
                                   f":{label} nodes")
        signor.run_batches(driver, db, signor.LOAD_SITES, list(sites.values()), args.batch_size, ":Site nodes")
        by_rel = defaultdict(list)
        for e in site_edges:
            by_rel[e["rel"]].append(e)
        for rel in signor.ENZYME_RELS.values():
            if by_rel[rel]:
                signor.run_batches(driver, db, LOAD_SITE_EDGES % rel, by_rel[rel], args.batch_size, f":{rel} edges")
        signor.run_batches(driver, db, signor.LOAD_REGULATES, regulates, args.batch_size, ":REGULATES edges")
        signor.run_batches(driver, db, LOAD_MODIFIES, modifies, args.batch_size, ":MODIFIES edges")
        driver.execute_query(
            "MERGE (m:Meta) SET m.dataset_version = $v, m.loaded_at = $t, m.counts_json = $c",
            v=version, t=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            c=json.dumps(counts), database_=db)
        print(f"Loaded dataset {version}.")


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signor", type=Path, default=SIGNOR_FILE)
    ap.add_argument("--kinet", type=Path, default=KINET_FILE)
    ap.add_argument("--groups", type=Path, default=GROUPS_FILE)
    ap.add_argument("--file", type=Path, help="alias of --signor (older docs)")
    ap.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    ap.add_argument("--user", default=os.getenv("NEO4J_USERNAME", "neo4j"))
    ap.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    ap.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--wipe", action="store_true", help="delete all nodes before loading")
    ap.add_argument("--if-outdated", action="store_true",
                    help="reload (wipe + load) only if the graph was built from different seed files")
    ap.add_argument("--if-empty", action="store_true", help="do nothing if the graph has any nodes")
    ap.add_argument("--wait", type=float, default=90, help="seconds to retry while Neo4j starts")
    ap.add_argument("--dry-run", action="store_true", help="parse, merge and print stats only")
    args = ap.parse_args(argv)
    if args.file:
        args.signor = args.file
    if not args.password and os.getenv("NEO4J_PASSWORD_FILE"):
        args.password = Path(os.environ["NEO4J_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()

    signor_parsed = signor.parse_file(args.signor)
    kinet_parsed = (parse_kinet(args.kinet) if args.kinet.is_file()
                    else ({}, {}, [], Counter({"missing file": 1})))
    groups = parse_groups(args.groups)
    entities, sites, site_edges, regulates, modifies = merge(signor_parsed, kinet_parsed, groups)
    version = dataset_version(args.signor, args.kinet, args.groups)
    counts = stats(entities, sites, site_edges, modifies)

    print(f"Dataset {version}: SIGNOR {args.signor.name}, KiNet {args.kinet.name if args.kinet.is_file() else '(missing)'}, "
          f"groups {args.groups.name if args.groups.is_file() else '(missing)'}")
    for k, v in counts.items():
        print(f"  {k:<24} {v}")
    if kinet_parsed[3]:
        print(f"  KiNet rows skipped: {dict(kinet_parsed[3])}")
    enzymes = {e["enzyme"] for e in modifies}
    with_group = sum(1 for e in enzymes if entities.get(e, {}).get("kinase_group"))
    print(f"  enzymes with a kinase group: {with_group}/{len(enzymes)}")

    if args.dry_run:
        return
    if not args.password:
        sys.exit("No password: pass --password or set NEO4J_PASSWORD")
    load(args, version, entities, sites, site_edges, regulates, modifies, counts)


if __name__ == "__main__":
    main()
