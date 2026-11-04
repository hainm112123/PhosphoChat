"""Merged loader and kinase-group builder, offline on small fixture files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_kinase_groups as kg  # noqa: E402
import load_graph  # noqa: E402
import load_signor  # noqa: E402

SIGNOR_TSV = """entityA\tentityA_name\tmechanism\teffect\tentityB\tentityB_name\tpmid\tsignor_id\tsignor-score
P17252\tPRKCA\tphosphorylation\tup-regulates\tQ96QS3_phSer174\tARX_phSer174\t30419043\tSIGNOR-277418\t0.4
Q96QS3_phSer174\tARX_phSer174\tphosphorylation\tdown-regulates activity\tQ96QS3\tARX\t30419043\tSIGNOR-277418\t0.4
"""

KINET_CSV = '''"Kinase","Kinase Name","Substrate","Substrate Name","Site","Source Database","Evidence","Reference (PMID)"
"P17252","PRKCA","Q96QS3","ARX","S174","PhosphoSitePlus","Mutation analysis","30419043; 11111111"
"","","","","","","",""
"P17252","PRKCA","Q96QS3","ARX","S174","EPSD","Unspecified experimental method",NA
"O00141","SGK1","O14920","IKBKB","S181","iPTMNet","Text mining","22222222"
"O00141","SGK1","O14920","IKBKB","R12","EPSD",NA,NA
'''

GROUPS_CSV = "uniprot,gene,group,family\nP17252,PRKCA,AGC,\"AGC Ser/Thr protein kinase family, PKC subfamily\"\n"


@pytest.fixture
def seed(tmp_path):
    (tmp_path / "s.tsv").write_text(SIGNOR_TSV, encoding="utf-8")
    (tmp_path / "k.csv").write_text(KINET_CSV, encoding="utf-8")
    (tmp_path / "g.csv").write_text(GROUPS_CSV, encoding="utf-8")
    return tmp_path


def _merged(seed):
    return load_graph.merge(load_signor.parse_file(seed / "s.tsv"),
                            load_graph.parse_kinet(seed / "k.csv"),
                            load_graph.parse_groups(seed / "g.csv"))


def test_parse_kinet(seed):
    entities, sites, records, skipped = load_graph.parse_kinet(seed / "k.csv")
    assert skipped == {"blank": 1, "residue": 1}
    assert "Q96QS3_phSer174" in sites and sites["Q96QS3_phSer174"]["position"] == 174
    epsd = next(r for r in records if r["source"] == "EPSD")
    assert epsd["pmids"] == [] and epsd["evidence"] == "Unspecified experimental method"


def test_merge_combines_sources_on_one_edge(seed):
    entities, sites, site_edges, regulates, modifies = _merged(seed)
    arx = next(e for e in modifies if e["enzyme"] == "P17252" and e["target"] == "Q96QS3")
    assert arx["sources"] == ["SIGNOR", "EPSD", "PhosphoSitePlus"] and arx["n_sources"] == 3
    assert arx["signor_ids"] == ["SIGNOR-277418"] and arx["score"] == 0.4
    assert arx["effect"] == "down-regulates activity"           # effect on the target, from SIGNOR
    assert arx["pmids"] == ["11111111", "30419043"]               # union across databases
    assert {r["source"] for r in json.loads(arx["records_json"])} == {"SIGNOR", "PhosphoSitePlus", "EPSD"}
    site = next(e for e in site_edges if e["site"] == "Q96QS3_phSer174")
    assert site["n_sources"] == 3 and site["rel"] == "PHOSPHORYLATES"

    ikbkb = next(e for e in modifies if e["target"] == "O14920")  # KiNet only
    assert ikbkb["sources"] == ["iPTMNet"] and ikbkb["score"] is None and ikbkb["effect"] is None
    assert entities["P17252"]["kinase_group"] == "AGC" and entities["O00141"]["kinase_group"] is None
    assert entities["Q96QS3"]["name"] == "ARX"


def test_dataset_version_changes_with_seed(seed):
    v1 = load_graph.dataset_version(seed / "s.tsv", seed / "k.csv", seed / "g.csv")
    (seed / "k.csv").write_text(KINET_CSV + '"P1","X","P2","Y","S1","EPSD",NA,NA\n', encoding="utf-8")
    assert load_graph.dataset_version(seed / "s.tsv", seed / "k.csv", seed / "g.csv") != v1


# --------------------------------------------------------------------------- kinase groups

@pytest.mark.parametrize("gene, families, group", [
    ("ATM", "PI3/PI4-kinase family, ATM subfamily", "Atypical"),
    ("PIK3CA", "PI3/PI4-kinase family", None),                   # lipid kinase: not a protein kinase
    ("CSNK2A1", "Protein kinase superfamily, Ser/Thr protein kinase family, CK2 subfamily", "CMGC"),
    ("NEK2", "Protein kinase superfamily, NEK Ser/Thr protein kinase family, NIMA subfamily", "Other"),
    ("SRC", "Protein kinase superfamily, Tyr protein kinase family, SRC subfamily", "TK"),
    ("ALPK1", "Protein kinase superfamily, Alpha-type protein kinase family, ALPK subfamily", "Atypical"),
    ("HK1", "Hexokinase family", None),
])
def test_kinase_group_rules(gene, families, group):
    assert kg.classify(gene, families) == group


def test_kinase_group_checks_catch_problems():
    good = [{"uniprot": "P12931", "gene": g, "group": grp, "family": ""} for g, grp in kg.SPOT_CHECKS.items()]
    filler = [{"uniprot": "P12931", "gene": f"K{i}", "group": "Other", "family": ""} for i in range(480)]
    assert kg.check(good + filler) == []
    wrong = [dict(r, group="TK") if r["gene"] == "CDK2" else r for r in good] + filler
    assert any("CDK2" in p for p in kg.check(wrong))
    assert any("accession" in p for p in kg.check(good + filler + [{"uniprot": "bad!", "gene": "X",
                                                                     "group": "TK", "family": ""}]))
    assert any("expected" in p and "kinome" in p for p in kg.check(good))   # too few


def test_kinase_groups_refuse_http():
    with pytest.raises(ValueError, match="non-HTTPS"):
        kg.fetch("http://www.kinhub.org/kinases.html")


def test_committed_kinase_groups_pass_the_checks():
    import csv
    rows = list(csv.DictReader((kg.SEED / "kinase_groups.csv").open(encoding="utf-8")))
    assert kg.check(rows) == []
