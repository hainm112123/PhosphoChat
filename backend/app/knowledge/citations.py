"""Check the citations in a chat answer against the vault and the graph.

Models slip when copying identifiers: a SIGNOR id with one digit changed, a PMID that isn't in
any record, a `[[page]]` that doesn't exist, or a link written as `[[id: page]]`. Code can check
every one of them cheaply, so the answer can say which citations could not be verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.vault.store import Vault

PAGE_RE = re.compile(r"\[\[(?!source:)([^\]|#]+)(?:[|#][^\]]*)?\]\]")
SOURCE_RE = re.compile(r"\[\[source:\s*([^\]]+?)\s*\]\]")
SIGNOR_RE = re.compile(r"SIGNOR-\d+")
PMID_RE = re.compile(r"PMID[:\s]*(\d{5,9})", re.I)


_REPAIRS = [  # (pattern, replacement), in this order; frontend Markdown.tsx::repairLinks does the same
    (re.compile(r"\[\[\s*source:\s*\[\[([^\]]+)\]\]\s*(?:\|\s*([^\]]+?))?\s*\]\]", re.I),
     lambda m: f"[[{m.group(1)}]]" + (f" [[source: {m.group(2)}]]" if m.group(2) else "")),
    (re.compile(r"\[{3,}"), "[["), (re.compile(r"\]{3,}"), "]]"),
    # [[page, source: file.md]] -> [[page]] [[source: file.md]]
    (re.compile(r"\[\[\s*([a-z0-9][a-z0-9-]*)\s*[,;]\s*source:\s*([^\]]+?)\s*\]\]", re.I), r"[[\1]] [[source: \2]]"),
    # [[id: page]], [[page-name: page]], [[synth: page]]...: any one-word label except "source"
    (re.compile(r"\[\[\s*(?!source\s*:)[a-z][a-z-]*\s*:\s*([a-z0-9][a-z0-9-]*)\s*\]\]", re.I), r"[[\1]]"),
    (re.compile(r"\[\[-+([^\]]+)\]\]"), r"[[\1]]"),
]


def repair_links(text: str) -> tuple[str, int]:
    """(text with link slips repaired, how many were repaired), as the UI shows it."""
    total = 0
    for pattern, repl in _REPAIRS:
        text, n = pattern.subn(repl, text)
        total += n
    return text, total


@dataclass
class CitationCheck:
    pages: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    signor_ids: set[str] = field(default_factory=set)
    pmids: set[str] = field(default_factory=set)
    invalid: list[str] = field(default_factory=list)   # as written in the answer
    graph_checked: bool = True
    format_slips: int = 0                              # malformed links the UI repairs

    def counts(self) -> dict:
        bad = set(self.invalid)
        return {"format_slips": self.format_slips, "pages": len(self.pages), "pages_valid": len(self.pages - bad),
                "sources": len(self.sources), "sources_valid": len(self.sources - bad),
                "signor_ids": len(self.signor_ids), "signor_valid": len(self.signor_ids - bad),
                "pmids": len(self.pmids), "pmids_valid": len(self.pmids - bad), "invalid": sorted(bad)}


def check_citations(answer: str, vault: Vault) -> CitationCheck:
    from app.graph import signor
    answer, slips = repair_links(answer)
    c = CitationCheck(format_slips=slips, pages={p.strip().lower() for p in PAGE_RE.findall(answer)},
                      sources={s.strip() for s in SOURCE_RE.findall(answer)},
                      signor_ids=set(SIGNOR_RE.findall(answer)), pmids=set(PMID_RE.findall(answer)))
    known_pages = set(vault.page_names())
    known_sources = {p.name for p in vault.sources_dir.iterdir()} if vault.sources_dir.is_dir() else set()
    bad = {p for p in c.pages if p not in known_pages} | {s for s in c.sources if s not in known_sources}
    unchecked_pmids = set(c.pmids)
    if unchecked_pmids:  # a PMID a source note quotes is fine too
        text = " ".join(p.read_text(encoding="utf-8", errors="replace") for p in vault.sources_dir.glob("*.md"))
        unchecked_pmids = {p for p in unchecked_pmids if p not in text}
    try:
        if c.signor_ids:
            rows = signor._run("UNWIND $ids AS s MATCH ()-[m:MODIFIES]->() WHERE s IN m.signor_ids "
                               "RETURN DISTINCT s", ids=sorted(c.signor_ids))
            bad |= c.signor_ids - {r["s"] for r in rows}
        if unchecked_pmids:
            rows = signor._run("UNWIND $ids AS p MATCH ()-[m:MODIFIES]->() WHERE p IN m.pmids "
                               "RETURN DISTINCT p", ids=sorted(unchecked_pmids))
            bad |= unchecked_pmids - {r["p"] for r in rows}
    except RuntimeError:  # graph down: say nothing rather than call good citations wrong
        c.graph_checked = False
    c.invalid = sorted(bad)
    return c


def unverified_note(c: CitationCheck) -> str | None:
    """A line to append to the answer, or None when every citation checks out."""
    if not c.invalid:
        return None
    shown = ", ".join(f"`{x}`" for x in c.invalid[:8])
    return (f"\n\n> [!note] Unverified citations\n> PhosphoChat could not find {shown} in your wiki, "
            "sources or the databases. Treat the statements citing them with care.")
