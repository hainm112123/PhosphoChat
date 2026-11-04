"""Build seed/kinase_groups.csv (UniProt -> Manning kinase group) from UniProt, over HTTPS.

    python scripts/build_kinase_groups.py            # fetch from rest.uniprot.org (HTTPS only)
    python scripts/build_kinase_groups.py --from f   # rebuild from a saved UniProt TSV

Source: reviewed human UniProtKB entries with keyword KW-0418 (Kinase), field "Protein
families". UniProt's family names follow the Manning et al. 2002 kinome groups for most
kinases; RULES map them, with documented exceptions (CK2 -> CMGC, PIKKs and other atypical
families -> Atypical). Metabolic and lipid kinases are left out: they are not protein kinases.
The result is checked (known kinases, group names, size) before anything is written, and the
provenance (URL, date, SHA-256) is written next to it in seed/kinase_groups.SOURCE.md.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

URL = ("https://rest.uniprot.org/uniprotkb/stream?format=tsv&fields=accession,gene_primary,"
       "protein_families&query=(organism_id:9606)%20AND%20(reviewed:true)%20AND%20(keyword:KW-0418)")
SEED = Path(__file__).resolve().parents[2] / "seed"
GROUPS = ["TK", "TKL", "STE", "CK1", "AGC", "CAMK", "CMGC", "Atypical", "Other", "RGC"]

PIKK = {"ATM", "ATR", "MTOR", "PRKDC", "SMG1", "TRRAP"}
ATYPICAL_FAMILIES = re.compile(r"Alpha-type protein kinase|PDK/BCKDK|ADCK protein kinase|RIO-type|"
                               r"BUD32 family|TAF1 family|WAL family", re.I)
RULES = [  # (pattern on the family text, group) - first match wins
    (re.compile(r"CK2 subfamily"), "CMGC"),
    (re.compile(r"\bAGC Ser/Thr"), "AGC"),
    (re.compile(r"\bCAMK Ser/Thr"), "CAMK"),
    (re.compile(r"\bCMGC Ser/Thr"), "CMGC"),
    (re.compile(r"\bSTE Ser/Thr"), "STE"),
    (re.compile(r"\bTKL Ser/Thr"), "TKL"),
    (re.compile(r"\bCK1 Ser/Thr"), "CK1"),
    (re.compile(r"\bTyr protein kinase family"), "TK"),
]
# Known answers the result must reproduce (Manning et al. 2002).
SPOT_CHECKS = {"SRC": "TK", "EGFR": "TK", "ABL1": "TK", "AKT1": "AGC", "PRKACA": "AGC",
               "PRKCA": "AGC", "CDK2": "CMGC", "MAPK1": "CMGC", "GSK3B": "CMGC", "CSNK2A1": "CMGC",
               "CAMK2A": "CAMK", "MAP2K1": "STE", "PAK1": "STE", "RAF1": "TKL", "BRAF": "TKL",
               "CSNK1D": "CK1", "ATM": "Atypical", "MTOR": "Atypical", "PRKDC": "Atypical",
               "AURKA": "Other", "PLK1": "Other"}
ACCESSION_RE = re.compile(r"[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}")
KINOME_SIZE = (450, 560)


def classify(gene: str, families: str) -> str | None:
    """Manning group for one UniProt entry, or None when it isn't a protein kinase."""
    if gene in PIKK or ATYPICAL_FAMILIES.search(families):
        return "Atypical"
    for pattern, group in RULES:
        if pattern.search(families):
            return group
    if "Protein kinase superfamily" in families:
        return "Other"
    return None  # metabolic / lipid kinase


def subfamily(families: str) -> str:
    parts = [p.strip() for p in families.split(";")[0].split(",")]
    return ", ".join(p for p in parts if p and p != "Protein kinase superfamily")


def build(raw: bytes) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8")), delimiter="\t"):
        gene = (r.get("Gene Names (primary)") or "").strip()
        fam = (r.get("Protein families") or "").strip()
        group = classify(gene, fam)
        if group:
            rows.append({"uniprot": r["Entry"].strip(), "gene": gene, "group": group,
                         "family": subfamily(fam)})
    return sorted(rows, key=lambda x: (GROUPS.index(x["group"]), x["gene"]))


def check(rows: list[dict]) -> list[str]:
    """Problems with a result (empty list = OK)."""
    by_gene = {r["gene"]: r["group"] for r in rows}
    problems = [f"{g}: expected {exp}, got {by_gene.get(g)}" for g, exp in SPOT_CHECKS.items()
                if by_gene.get(g) != exp]
    bad_acc = [r["uniprot"] for r in rows if not ACCESSION_RE.fullmatch(r["uniprot"])]
    if bad_acc:
        problems.append(f"invalid UniProt accessions: {bad_acc[:5]}")
    if any(r["group"] not in GROUPS for r in rows):
        problems.append("unknown group name")
    if not KINOME_SIZE[0] <= len(rows) <= KINOME_SIZE[1]:
        problems.append(f"{len(rows)} protein kinases; expected {KINOME_SIZE[0]}-{KINOME_SIZE[1]} (the human kinome)")
    return problems


def fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("Refusing a non-HTTPS source.")
    with urllib.request.urlopen(url, timeout=60) as resp:  # TLS certificate is verified
        return resp.read()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", type=Path, help="a UniProt TSV saved earlier")
    ap.add_argument("--out", type=Path, default=SEED)
    args = ap.parse_args()
    if args.src:
        raw, origin = args.src.read_bytes(), f"{URL} (saved response {args.src.name})"
    else:
        raw, origin = fetch(URL), URL
    rows = build(raw)
    problems = check(rows)
    if problems:
        sys.exit("Kinase group checks failed, nothing written:\n  " + "\n  ".join(problems))
    out = args.out / "kinase_groups.csv"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["uniprot", "gene", "group", "family"], lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    out.write_text(buf.getvalue(), encoding="utf-8")
    counts = {g: sum(r["group"] == g for r in rows) for g in GROUPS}
    (args.out / "kinase_groups.SOURCE.md").write_text(
        "# kinase_groups.csv provenance\n\n"
        f"- Source: UniProtKB REST API over HTTPS: {origin}\n"
        f"- Retrieved: {date.today().isoformat()}\n"
        f"- SHA-256 of the UniProt response: `{hashlib.sha256(raw).hexdigest()}`\n"
        f"- SHA-256 of kinase_groups.csv: `{hashlib.sha256(out.read_bytes()).hexdigest()}`\n"
        "- Rebuild: `python backend/scripts/build_kinase_groups.py` (mapping rules are in that file)\n"
        "- Groups follow Manning G. et al. (2002) The protein kinase complement of the human "
        "genome. Science 298:1912-1934.\n"
        f"- Counts: {', '.join(f'{g} {n}' for g, n in counts.items())} (total {len(rows)})\n"
        f"- Spot checks passed: {', '.join(f'{g}={e}' for g, e in SPOT_CHECKS.items())}\n",
        encoding="utf-8")
    print(f"Wrote {out} ({len(rows)} kinases): " + ", ".join(f"{g} {n}" for g, n in counts.items()))


if __name__ == "__main__":
    main()
