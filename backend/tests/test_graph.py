"""Port of mcp/scripts/smoke_test_server.py, calling the graph functions directly.

Needs Neo4j loaded with PhosphoSIGNOR (NEO4J_* in .env); skipped otherwise.
"""

from __future__ import annotations

import pytest

from app.graph import signor

pytestmark = [pytest.mark.neo4j, pytest.mark.usefixtures("neo4j_ready")]


def test_overview():
    assert signor.graph_overview()["relationships"].get("MODIFIES", 0) > 13000


def test_search_finds_mapk1():
    assert any(x["name"] == "MAPK1" for x in signor.search_entities("mapk")["results"])


def test_regulators_of_arx():
    r = signor.regulators_of("arx")
    assert any(x["enzyme"] == "PRKCA" and "Ser174" in x["sites"]
               and x["signor_id"] == "SIGNOR-277418" for x in r["results"])


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
