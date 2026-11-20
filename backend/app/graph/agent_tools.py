"""The graph tools a chat model sees: few, with only the arguments it needs.

Every optional filter in a tool's schema is sent with every request, so the model-facing
tools take names only and apply sensible defaults (all databases, any score). The Graph
explorer and the retrieved context call `signor.py` directly, with every filter.
"""

from __future__ import annotations

import re
from typing import Literal

from app.graph import signor

LIMIT = 25
_RESIDUES = {"ser": "S", "thr": "T", "tyr": "Y", "his": "H"}


def _site_key(site: str) -> tuple[str, str] | None:
    m = re.fullmatch(r"\s*(ser|thr|tyr|his|[stygh])-?(\d+)\s*", site, re.I)
    if not m:
        return None
    r = m.group(1).lower()
    return _RESIDUES.get(r, r.upper()), m.group(2)


def search_entities(name: str) -> dict:
    """Find proteins, complexes or families by (partial) name or ID, when unsure of the gene symbol."""
    return signor.search_entities(name, 10)


def interactions(protein: str, direction: Literal["in", "out"] = "in", site: str | None = None) -> dict:
    """Database links of a protein: "in" = kinases/phosphatases that modify it, "out" = its substrates.

    Args:
        protein: gene symbol or UniProt id, e.g. 'TP53'.
        direction: "in" (who modifies it) or "out" (what it modifies).
        site: only links at this residue, e.g. 'S15'.
    """
    fn = signor.regulators_of if direction == "in" else signor.targets_of
    data = fn(protein, limit=signor.MAX_LIMIT if site else LIMIT)
    data["direction"] = direction
    ent = signor._resolve(protein)[0]
    data["kinase_group"] = ent.get("kinase_group")
    if site:
        want = _site_key(site)
        if want is None:
            raise ValueError(f"'{site}' is not a site; use a residue and position such as 'S15'.")
        data["results"] = [r for r in data["results"]
                           if any(_site_key(signor._site_label(s)) == want for s in r.get("sites") or [])][:LIMIT]
        data["site"] = site
    return data


def find_paths(source: str, target: str) -> dict:
    """Shortest signalling paths (up to 3 hops) from one protein to another, through enzyme -> substrate links."""
    return signor.find_paths(source, target)


def get_interaction(enzyme: str, target: str) -> dict:
    """The database records behind "enzyme modifies target": databases, evidence, PMIDs, sites, SIGNOR id."""
    return signor.get_interaction(enzyme, target)


GRAPH_TOOLS = [search_entities, interactions, find_paths, get_interaction]
