"""Port of mcp/scripts/smoke_test_server.py, calling the graph functions directly.

Needs Neo4j loaded with PhosphoSIGNOR (NEO4J_* in .env); skipped otherwise.
"""

from __future__ import annotations

import pytest

from app.graph import signor

pytestmark = [pytest.mark.neo4j, pytest.mark.usefixtures("neo4j_ready")]


def test_overview():
    o = signor.graph_overview()
    assert o["relationships"].get("MODIFIES", 0) > 10000          # one merged link per interaction
    assert set(o["links_per_source"]) == {"SIGNOR", "PhosphoSitePlus", "EPSD", "iPTMNet"}
    assert o["dataset"]["version"]


def test_search_finds_mapk1():
    assert any(x["name"] == "MAPK1" for x in signor.search_entities("mapk")["results"])


def test_regulators_of_arx():
    r = signor.regulators_of("arx")
    hit = next(x for x in r["results"] if x["enzyme"] == "PRKCA")
    assert "Ser174" in hit["sites"] and hit["signor_id"] == "SIGNOR-277418" and "SIGNOR" in hit["sources"]


def test_regulators_of_tp53_min_score():
    r = signor.regulators_of("TP53", mechanism="phosphorylation", min_score=0.5)
    assert any(x["enzyme"] == "CDK2" and "Ser315" in x["sites"] for x in r["results"])
    assert all(x["score"] >= 0.5 for x in r["results"])


def test_lookup_by_uniprot():
    assert signor.regulators_of("P04637")["matched_ids"] == ["P04637"]


def test_targets_of_phosphatase():
    assert signor.targets_of("PTPN1", mechanism="dephosphorylation")["total"] > 0


def test_mapt_isoforms():
    assert len(signor.sites_of("MAPT")["matched_ids"]) == 4


def test_get_entity_tp53():
    r = signor.get_entity("TP53")
    assert "Ser15" in r["phosphosites"] and r["top_regulators"]


def test_find_paths():
    assert isinstance(signor.find_paths("EGFR", "MYC", max_hops=3)["paths"], list)


def test_get_evidence_bare_number():
    r = signor.get_evidence("277418")
    assert r["pmids"] and r["pubmed_urls"]


def test_unknown_name_mentions_search():
    with pytest.raises(ValueError, match="search_entities"):
        signor.regulators_of("NOTAPROTEIN123")


def test_near_miss_suggests_tp53():
    with pytest.raises(ValueError, match="TP53"):
        signor.regulators_of("TP5")


# --------------------------------------------------------------------------- merged sources

def _kinet_only_link():
    rows = signor._run("""MATCH (a:Entity)-[m:MODIFIES]->(b:Entity)
        WHERE NOT 'SIGNOR' IN m.sources AND m.n_sources >= 2
        RETURN a.name AS enzyme, b.name AS target, m.sources AS sources
        ORDER BY a.name, b.name LIMIT 1""")
    assert rows, "expected links reported only by KiNet databases"
    return rows[0]


def test_kinet_only_link_and_source_filter():
    link = _kinet_only_link()
    r = signor.regulators_of(link["target"])
    hit = [x for x in r["results"] if x["enzyme"] == link["enzyme"]]
    assert hit and "SIGNOR" not in hit[0]["sources"] and hit[0]["score"] is None
    only_signor = signor.regulators_of(link["target"], sources=["SIGNOR"])
    assert all("SIGNOR" in x["sources"] for x in only_signor["results"])
    assert not any(x["enzyme"] == link["enzyme"] for x in only_signor["results"])
    # min_score > 0 keeps only SIGNOR-scored links
    assert all(x["score"] is not None for x in signor.regulators_of(link["target"], min_score=0.1)["results"])


def test_get_interaction_lists_records_per_database():
    link = _kinet_only_link()
    r = signor.get_interaction(link["enzyme"], link["target"])
    dbs = {rec["source"] for i in r["interactions"] for rec in i["records"]}
    assert dbs == set(link["sources"]) and len(dbs) >= 2
    with pytest.raises(ValueError, match="No modification"):
        signor.get_interaction("TP53", "EGFR")


def test_kinase_groups_on_entities():
    assert signor.get_entity("SRC")["kinase_group"] == "TK"
    assert signor.get_entity("CDK2")["kinase_group"] == "CMGC"
    assert signor.get_entity("TP53")["kinase_group"] is None
