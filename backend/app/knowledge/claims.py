"""Claims: kinase/phosphatase -> substrate statements found in a vault's sources.

A claim is recorded only when it's grounded: both proteins resolve to graph entities and the
supporting quote appears verbatim in the cited source. Claims live in the vault
(`relations.jsonl`, git-versioned, rolled back with a failed job) and are projected into Neo4j
as their own edge type, CLAIMED, never mixed into the curated database edges.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

from app.graph import signor
from app.vault.store import Vault, VaultError, split_frontmatter

Status = Literal["confirmed", "new", "conflicting"]
SITE_RE = re.compile(r"^(?:(Ser|Thr|Tyr|His)|([STYH]))(\d{1,5})$", re.I)
RESIDUE = {"s": "Ser", "t": "Thr", "y": "Tyr", "h": "His"}
EFFECTS = ("up-regulates", "down-regulates", "unknown")
MAX_QUOTE = 300


def _norm(text: str) -> str:
    """Comparison form: lowercase, plain punctuation, no markdown emphasis, single spaces."""
    t = text.lower()
    for a, b in {"‑": "-", "‐": "-", "–": "-", "—": "-", "’": "'",
                 "‘": "'", "“": '"', "”": '"', " ": " "}.items():
        t = t.replace(a, b)
    t = re.sub(r"[*_`]", "", t)
    return " ".join(t.split())


def normalize_site(site: str | None) -> str | None:
    if not site or not site.strip():
        return None
    m = SITE_RE.match(site.strip())
    if not m:
        raise VaultError(f"Site '{site}' should look like Ser81 (or S81, T202, Y15).")
    res = m.group(1) or RESIDUE[m.group(2).lower()]
    return f"{res.capitalize()}{int(m.group(3))}"


def _effect(effect: str | None) -> str:
    e = (effect or "unknown").strip().lower()
    if e.startswith("up"):
        return "up-regulates"
    if e.startswith("down"):
        return "down-regulates"
    return "unknown"


def _resolve_one(name: str, role: str) -> dict:
    try:
        ents = signor._resolve(name)
    except ValueError as e:
        raise VaultError(f"{role} '{name}': {e}") from e
    return ents[0]


def record(vault: Vault, *, enzyme: str, target: str, site: str | None, mechanism: str,
           effect: str | None, source: str, quote: str, page: str | None = None) -> tuple[dict, bool]:
    """Validate and store one claim. Returns (claim, added); added is False for a duplicate."""
    if mechanism not in ("phosphorylation", "dephosphorylation"):
        raise VaultError("mechanism must be 'phosphorylation' or 'dephosphorylation'.")
    e, t = _resolve_one(enzyme, "enzyme"), _resolve_one(target, "target")
    site_n = normalize_site(site)
    src = source.strip().removeprefix("sources/").removeprefix("[[source:").removesuffix("]]").strip()
    path = vault.sources_dir / src
    if not src or not path.is_file() or path.suffix != ".md":
        raise VaultError(f"Unknown source '{source}'. Use the exact source note filename.")
    q = " ".join(quote.split())
    if not q or len(q) > MAX_QUOTE:
        raise VaultError(f"quote must be the supporting sentence from the source (1-{MAX_QUOTE} characters).")
    _, body = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    if _norm(q) not in _norm(body):
        raise VaultError("The quote doesn't appear in the source. Copy the sentence exactly as the "
                         "source writes it (no paraphrase).")
    key = "|".join([e["id"], t["id"], site_n or "", mechanism, src])
    claim = {
        "id": hashlib.sha256(key.encode()).hexdigest()[:12],
        "enzyme": e["name"], "enzyme_id": e["id"], "target": t["name"], "target_id": t["id"],
        "site": site_n, "mechanism": mechanism, "effect": _effect(effect),
        "source": src, "quote": q, "page": page,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review": "unreviewed",
    }
    added = vault.add_relation(claim)
    return claim, added


# --------------------------------------------------------------------------- status vs databases

def statuses(claims: list[dict]) -> list[dict]:
    """Each claim with its status against the merged databases (computed at read time)."""
    if not claims:
        return []
    pairs = [{"e": c["enzyme_id"], "t": c["target_id"], "m": c["mechanism"]} for c in claims]
    try:
        rows = signor._run("""
            UNWIND $pairs AS p
            MATCH (a:Entity {id: p.e})-[r:MODIFIES {mechanism: p.m}]->(b:Entity {id: p.t})
            RETURN p.e AS e, p.t AS t, p.m AS m, r.sites AS sites, r.sources AS sources,
                   r.effect AS effect, head(r.signor_ids) AS signor_id""", pairs=pairs)
        graph_ok = True
    except RuntimeError:
        rows, graph_ok = [], False
    links = {(r["e"], r["t"], r["m"]): r for r in rows}
    out = []
    for c in claims:
        link = links.get((c["enzyme_id"], c["target_id"], c["mechanism"]))
        item = dict(c)
        if not graph_ok:
            item.update(status="unknown", detail="graph unavailable")
        elif not link:
            item.update(status="new", detail="no database reports this link")
        else:
            sites = {signor._site_label(s) for s in link["sites"] or []}
            item["databases"], item["signor_id"] = link["sources"], link["signor_id"]
            db_effect = _effect(link["effect"])
            if c["effect"] != "unknown" and db_effect != "unknown" and c["effect"] != db_effect:
                item.update(status="conflicting",
                            detail=f"SIGNOR says {link['effect']}; your source says {c['effect']}")
            elif c.get("site") and c["site"] not in sites:
                item.update(status="new", detail=f"link known ({', '.join(link['sources'])}), "
                                                 f"but not at {c['site']} (known: {', '.join(sorted(sites)) or 'none'})")
            else:
                item.update(status="confirmed", detail=f"reported by {', '.join(link['sources'])}")
        out.append(item)
    return out


def list_claims(vault: Vault, protein: str | None = None, status: str | None = None,
                include_rejected: bool = False) -> list[dict]:
    claims = [c for c in vault.read_relations() if include_rejected or c.get("review") != "rejected"]
    if protein:
        try:
            ids = {e["id"] for e in signor._resolve(protein)}
        except (ValueError, RuntimeError):
            ids = set()
        p = protein.strip().upper()
        claims = [c for c in claims if c["enzyme_id"] in ids or c["target_id"] in ids
                  or c["enzyme"].upper() == p or c["target"].upper() == p]
    out = statuses(claims)
    return [c for c in out if not status or c["status"] == status]


# --------------------------------------------------------------------------- projection

def sync_claims(vault: Vault) -> int:
    """Replace this vault's CLAIMED edges in Neo4j with the claims in relations.jsonl.
    The only write the app makes to the graph; curated database edges are never touched."""
    claims = [c for c in vault.read_relations() if c.get("review") != "rejected"]
    rows = [{k: c.get(k) for k in ("id", "enzyme_id", "target_id", "site", "mechanism", "effect",
                                   "source", "page", "quote", "review")} for c in claims]
    s = signor.get_settings()
    driver = signor._get_driver()
    driver.execute_query("MATCH ()-[r:CLAIMED {vault: $v}]->() DELETE r", v=vault.name,
                         database_=s.neo4j_database)
    if rows:
        driver.execute_query("""
            UNWIND $rows AS c
            MATCH (a:Entity {id: c.enzyme_id}) MATCH (b:Entity {id: c.target_id})
            CREATE (a)-[:CLAIMED {vault: $v, claim_id: c.id, site: c.site, mechanism: c.mechanism,
                                  effect: c.effect, source: c.source, page: c.page, quote: c.quote,
                                  review: c.review}]->(b)""", rows=rows, v=vault.name,
                             database_=s.neo4j_database)
    return len(rows)


def safe_sync(vault: Vault) -> int | None:
    """sync_claims that never fails the caller (Neo4j down at startup or after a job)."""
    try:
        return sync_claims(vault)
    except Exception:  # noqa: BLE001
        return None
