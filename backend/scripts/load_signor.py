"""Load PhosphoSIGNOR data into Neo4j.

Graph model
-----------
    (:Entity:Protein|Complex|ProteinFamily|FusionProtein|Phenotype {id, name})
        -[:PHOSPHORYLATES|DEPHOSPHORYLATES {signor_id, effect, pmids, score}]->
    (:Site {id, name, residue, position, protein_id})
        -[:REGULATES {signor_id, mechanism, effect, pmids, score}]->(:Entity)
    (:Site)-[:SITE_OF]->(:Entity)

    Shortcut edge for multi-hop path queries between entities:
    (:Entity)-[:MODIFIES {signor_id, mechanism, sites, enzyme_effect,
                          target_effect, pmids, score}]->(:Entity)

Each SIGNOR record (signor_id) is two TSV rows: enzyme -> site, site -> protein.

Usage (from backend/)
-----
    python scripts/load_signor.py --file <tsv> --dry-run    # parse + stats only
    python scripts/load_signor.py --file <tsv>              # load
    python scripts/load_signor.py --file <tsv> --wipe       # clear graph first

Connection settings default to NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD /
NEO4J_DATABASE, read from the environment or the repo-root .env.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass

# Sibling checkout of the original MCP project; pass --file to load from anywhere else.
DEFAULT_FILE = Path(__file__).resolve().parents[3] / "mcp" / "data" / "PhosphoSIGNOR_all_Data.tsv"

SITE_RE = re.compile(r"^(?P<base>[^_]+)_ph(?P<rest>.+)$")
RESIDUE_RE = re.compile(r"(Ser|Thr|Tyr|His)\s*(\d+)")

# SIGNOR ID prefix -> node label. Anything else is treated as a UniProt protein.
SIGNOR_LABELS = {
    "SIGNOR-C": "Complex",
    "SIGNOR-PF": "ProteinFamily",
    "SIGNOR-FP": "FusionProtein",
    "SIGNOR-PH": "Phenotype",
}
ENTITY_LABELS = ["Protein", *SIGNOR_LABELS.values()]
ENZYME_RELS = {"phosphorylation": "PHOSPHORYLATES", "dephosphorylation": "DEPHOSPHORYLATES"}


# --------------------------------------------------------------------------- parsing

def entity_label(entity_id: str) -> str:
    for prefix, label in SIGNOR_LABELS.items():
        if entity_id.startswith(prefix):
            return label
    return "Protein"


def parse_sites(raw_id: str, raw_name: str) -> list[dict] | None:
    """Split a site ID like 'Q02548_phSer189; Ser283' into one dict per residue.

    Returns None if the ID is not a phosphosite.
    """
    m = SITE_RE.match(raw_id.strip())
    if not m:
        return None
    base = m.group("base")
    gene = raw_name.strip().split("_ph")[0]
    residues = RESIDUE_RE.findall(m.group("rest"))
    if not residues:
        raise ValueError(f"Unparseable site ID: {raw_id!r}")
    return [
        {
            "id": f"{base}_ph{res}{pos}",
            "name": f"{gene}_ph{res}{pos}",
            "residue": res,
            "position": int(pos),
            "protein_id": base,
            "protein_name": gene,
        }
        for res, pos in residues
    ]


def parse_pmids(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(";") if p.strip()]


def parse_file(path: Path):
    entities: dict[str, dict] = {}
    sites: dict[str, dict] = {}
    enzyme_edges: list[dict] = []
    regulates_edges: list[dict] = []
    # (signor_id, site ids) -> partial record, used to build MODIFIES shortcut edges.
    # Keyed by sites too: a few signor_ids are reused for different sites (the raw
    # file distinguishes them only by a trailing non-breaking space).
    records: dict[tuple, dict] = defaultdict(dict)
    warnings: list[str] = []

    def add_entity(eid: str, name: str):
        eid, name = eid.strip(), name.strip()
        entities.setdefault(eid, {"id": eid, "name": name, "label": entity_label(eid)})
        return eid

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for lineno, row in enumerate(reader, start=2):
            a_sites = parse_sites(row["entityA"], row["entityA_name"])
            b_sites = parse_sites(row["entityB"], row["entityB_name"])
            mechanism = row["mechanism"].strip()
            signor_id = row["signor_id"].strip()
            common = {
                "signor_id": signor_id,
                "effect": row["effect"].strip(),
                "pmids": parse_pmids(row["pmid"]),
                "score": float(row["signor-score"]),
            }

            if a_sites is None and b_sites is not None:
                # enzyme -> site
                enzyme = add_entity(row["entityA"], row["entityA_name"])
                for s in b_sites:
                    sites.setdefault(s["id"], s)
                    enzyme_edges.append({**common, "rel": ENZYME_RELS[mechanism],
                                         "enzyme": enzyme, "site": s["id"]})
                site_ids = tuple(s["id"] for s in b_sites)
                records[(signor_id, site_ids)].update(
                    enzyme=enzyme, mechanism=mechanism, enzyme_effect=common["effect"],
                    pmids=common["pmids"], score=common["score"])
            elif a_sites is not None and b_sites is None:
                # site -> regulated entity
                target = add_entity(row["entityB"], row["entityB_name"])
                for s in a_sites:
                    sites.setdefault(s["id"], s)
                    if s["protein_id"] != target:
                        warnings.append(f"line {lineno}: site {s['id']} regulates {target}")
                    regulates_edges.append({**common, "mechanism": mechanism,
                                            "site": s["id"], "target": target})
                site_ids = tuple(s["id"] for s in a_sites)
                records[(signor_id, site_ids)].update(target=target, target_effect=common["effect"])
            else:
                warnings.append(f"line {lineno}: skipped, unexpected shape "
                                f"{row['entityA']!r} -> {row['entityB']!r}")

    # Every site belongs to its base protein, even if no row says so explicitly.
    for s in sites.values():
        entities.setdefault(s["protein_id"], {"id": s["protein_id"], "name": s["protein_name"],
                                              "label": "Protein"})

    # One MODIFIES edge per (signor_id, enzyme, target), collecting all its sites.
    modifies: dict[tuple, dict] = {}
    for (sid, site_ids), rec in records.items():
        if "enzyme" not in rec or "target" not in rec:
            warnings.append(f"{sid} {list(site_ids)}: incomplete record "
                            f"(missing enzyme or target row)")
            continue
        edge = modifies.setdefault((sid, rec["enzyme"], rec["target"]),
                                   {"signor_id": sid, **rec, "sites": []})
        edge["sites"] += [s for s in site_ids if s not in edge["sites"]]
    modifies_edges = list(modifies.values())

    return entities, sites, enzyme_edges, regulates_edges, modifies_edges, warnings


# --------------------------------------------------------------------------- loading

SCHEMA = [
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT site_id IF NOT EXISTS FOR (s:Site) REQUIRE s.id IS UNIQUE",
    "CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)",
    "CREATE INDEX site_name IF NOT EXISTS FOR (s:Site) ON (s.name)",
]

LOAD_ENTITIES = """
UNWIND $rows AS row
MERGE (n:Entity {id: row.id})
SET n:%s, n.name = row.name
"""

LOAD_SITES = """
UNWIND $rows AS row
MERGE (s:Site {id: row.id})
SET s.name = row.name, s.residue = row.residue, s.position = row.position,
    s.protein_id = row.protein_id
WITH s, row
MATCH (p:Entity {id: row.protein_id})
MERGE (s)-[:SITE_OF]->(p)
"""

LOAD_ENZYME_EDGES = """
UNWIND $rows AS row
MATCH (e:Entity {id: row.enzyme})
MATCH (s:Site {id: row.site})
MERGE (e)-[r:%s {signor_id: row.signor_id}]->(s)
SET r.effect = row.effect, r.pmids = row.pmids, r.score = row.score
"""

LOAD_REGULATES = """
UNWIND $rows AS row
MATCH (s:Site {id: row.site})
MATCH (t:Entity {id: row.target})
MERGE (s)-[r:REGULATES {signor_id: row.signor_id}]->(t)
SET r.mechanism = row.mechanism, r.effect = row.effect, r.pmids = row.pmids,
    r.score = row.score
"""

LOAD_MODIFIES = """
UNWIND $rows AS row
MATCH (e:Entity {id: row.enzyme})
MATCH (t:Entity {id: row.target})
MERGE (e)-[r:MODIFIES {signor_id: row.signor_id}]->(t)
SET r.mechanism = row.mechanism, r.sites = row.sites,
    r.enzyme_effect = row.enzyme_effect, r.target_effect = row.target_effect,
    r.pmids = row.pmids, r.score = row.score
"""


def batched(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def run_batches(driver, db: str, query: str, rows: list, size: int, what: str):
    for chunk in batched(rows, size):
        driver.execute_query(query, rows=chunk, database_=db)
    print(f"  {what}: {len(rows)}")


def load(args, entities, sites, enzyme_edges, regulates_edges, modifies_edges):
    try:
        from neo4j import GraphDatabase
    except ImportError:
        sys.exit("The neo4j driver is not installed: pip install -r requirements.txt")

    with GraphDatabase.driver(args.uri, auth=(args.user, args.password)) as driver:
        driver.verify_connectivity()
        db = args.database

        if args.wipe:
            print("Wiping existing graph ...")
            # CALL ... IN TRANSACTIONS needs an auto-commit transaction (session.run)
            with driver.session(database=db) as session:
                session.run("MATCH (n) CALL { WITH n DETACH DELETE n } "
                            "IN TRANSACTIONS OF 10000 ROWS").consume()

        for stmt in SCHEMA:
            driver.execute_query(stmt, database_=db)

        print("Loading ...")
        by_label = defaultdict(list)
        for e in entities.values():
            by_label[e["label"]].append(e)
        for label in ENTITY_LABELS:  # whitelist: labels are interpolated into Cypher
            if by_label[label]:
                run_batches(driver, db, LOAD_ENTITIES % label, by_label[label],
                            args.batch_size, f":{label} nodes")
        run_batches(driver, db, LOAD_SITES, list(sites.values()), args.batch_size, ":Site nodes")

        by_rel = defaultdict(list)
        for e in enzyme_edges:
            by_rel[e["rel"]].append(e)
        for rel in ENZYME_RELS.values():
            if by_rel[rel]:
                run_batches(driver, db, LOAD_ENZYME_EDGES % rel, by_rel[rel],
                            args.batch_size, f":{rel} edges")
        run_batches(driver, db, LOAD_REGULATES, regulates_edges, args.batch_size, ":REGULATES edges")
        run_batches(driver, db, LOAD_MODIFIES, modifies_edges, args.batch_size, ":MODIFIES edges")

        print("\nGraph now contains:")
        records, _, _ = driver.execute_query(
            "MATCH (n) UNWIND labels(n) AS l RETURN l, count(*) AS c ORDER BY l", database_=db)
        for r in records:
            print(f"  :{r['l']:<16} {r['c']}")
        records, _, _ = driver.execute_query(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY t", database_=db)
        for r in records:
            print(f"  :{r['t']:<16} {r['c']}")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    ap.add_argument("--user", default=os.getenv("NEO4J_USERNAME", "neo4j"))
    ap.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    ap.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--wipe", action="store_true", help="delete all nodes before loading")
    ap.add_argument("--dry-run", action="store_true", help="parse and print stats; no database")
    args = ap.parse_args()

    entities, sites, enzyme_edges, regulates_edges, modifies_edges, warnings = parse_file(args.file)

    print(f"Parsed {args.file.name}")
    for label, n in sorted(Counter(e["label"] for e in entities.values()).items()):
        print(f"  {label:<16} {n}")
    print(f"  {'Site':<16} {len(sites)}")
    for rel, n in sorted(Counter(e["rel"] for e in enzyme_edges).items()):
        print(f"  {rel:<16} {n}")
    print(f"  {'REGULATES':<16} {len(regulates_edges)}")
    print(f"  {'MODIFIES':<16} {len(modifies_edges)}")
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings[:20]:
            print(f"  {w}")

    if args.dry_run:
        return
    if not args.password:
        sys.exit("No password: pass --password or set NEO4J_PASSWORD")
    print()
    load(args, entities, sites, enzyme_edges, regulates_edges, modifies_edges)


if __name__ == "__main__":
    main()
