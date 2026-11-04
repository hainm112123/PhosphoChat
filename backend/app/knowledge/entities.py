"""Entity linking: which graph proteins a piece of text (a question, a wiki page) mentions.

Matching is exact against the graph's own names and UniProt ids, so it never invents
proteins; a short stoplist removes common uppercase words that happen to be gene symbols.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from app.graph import signor
from app.vault.store import Vault, split_frontmatter

TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9-]{1,14}(?![A-Za-z0-9_-])")
UNIPROT_RE = re.compile(r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})(-\d+)?$")
# Uppercase terms that match a graph name but, written in caps, almost never mean a protein.
# (Lowercase prose words are already ignored, so real proteins like MET or MAX are kept.)
STOPLIST = {"DNA", "RNA", "ATP", "ADP", "GTP", "GDP", "AMP", "PCR", "LPS", "USA", "PDF",
            "ID", "II", "IV", "WT", "KO", "SD", "MS", "NA", "OK"}


@dataclass
class Mention:
    text: str
    ids: list[str]
    name: str = ""


@dataclass
class NameIndex:
    version: str | None = None
    by_key: dict[str, list[str]] = field(default_factory=dict)   # UPPER(name or id) -> ids
    names: dict[str, str] = field(default_factory=dict)          # id -> display name


_index = NameIndex()
_lock = threading.Lock()


def graph_names(force: bool = False) -> NameIndex:
    """Every Entity name and id, loaded once and reloaded when the dataset version changes."""
    global _index
    with _lock:
        try:
            meta = signor._run("MATCH (m:Meta) RETURN m.dataset_version AS v LIMIT 1")
            version = meta[0]["v"] if meta else "none"
        except RuntimeError:
            return _index  # graph down: keep what we have (possibly empty)
        if _index.by_key and _index.version == version and not force:
            return _index
        rows = signor._run("MATCH (n:Entity) RETURN n.id AS id, n.name AS name")
        idx = NameIndex(version=version)
        for r in rows:
            idx.names[r["id"]] = r["name"]
            for key in {str(r["id"]).upper(), str(r["name"] or "").upper()}:
                if key:
                    idx.by_key.setdefault(key, []).append(r["id"])
        _index = idx
        return _index


def extract_mentions(text: str, index: NameIndex | None = None, limit: int = 50) -> list[Mention]:
    """Graph proteins mentioned in text, in order of first appearance, de-duplicated."""
    idx = index or graph_names()
    if not idx.by_key or not text:
        return []
    found: dict[tuple[str, ...], Mention] = {}
    for m in TOKEN_RE.finditer(text):
        tok = m.group(0)
        key = tok.upper()
        if key in STOPLIST or key not in idx.by_key:
            continue
        # Lowercase prose words ("met", "rest") only count when written like a symbol (with a
        # digit, e.g. "tp53") or an id.
        if tok.islower() and not any(c.isdigit() for c in tok) and not UNIPROT_RE.match(key):
            continue
        ids = tuple(sorted(idx.by_key[key]))
        if ids not in found:
            found[ids] = Mention(text=tok, ids=list(ids), name=idx.names.get(ids[0], tok))
            if len(found) >= limit:
                break
    return list(found.values())


# --------------------------------------------------------------------------- vault index

_vault_cache: dict[str, tuple[tuple, str | None, dict[str, list[str]]]] = {}


def _wiki_signature(vault: Vault) -> tuple:
    return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in vault.wiki_dir.glob("*.md")))


def entity_index(vault: Vault) -> dict[str, list[str]]:
    """{entity_id: [page names]} for a vault, rebuilt when any wiki page changes."""
    idx = graph_names()
    sig = _wiki_signature(vault)
    cached = _vault_cache.get(str(vault.root))
    if cached and cached[0] == sig and cached[1] == idx.version:
        return cached[2]
    out: dict[str, list[str]] = {}
    for name in vault.page_names():
        try:
            meta, body = split_frontmatter(vault.read_page(name))
        except Exception:  # noqa: BLE001 - a broken page shouldn't break linking
            continue
        text = f"{meta.get('title', '')}\n{meta.get('summary', '')}\n{body}"
        for mention in extract_mentions(text, idx, limit=200):
            for i in mention.ids:
                out.setdefault(i, [])
                if name not in out[i]:
                    out[i].append(name)
    _vault_cache[str(vault.root)] = (sig, idx.version, out)
    return out
