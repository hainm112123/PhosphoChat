"""Score the wiki and claims a capture run produced.

    cd backend && .venv/Scripts/python -m eval.score_capture --vault eval-gemini-flash

Per paper:
- how much of the source the reader was given (sections kept, parts read or failed);
- page statistics (words, citations, links, uncited blocks) and mechanical lint;
- groundedness: unsupported statements found by the judge, given the FULL source;
- coverage of the paper's main findings in the wiki (judge);
- claims: status against the databases, judge labels, and a recall proxy (sentences in the
  source that say "X phosphorylates Y" about two graph proteins).
Writes results/score-capture-<vault>.json. Resumable per paper.
"""

from __future__ import annotations

import argparse
import asyncio
import re

from eval.common import RESULTS, add_groq_key_arg, load, save, slug, use_groq_key
from eval.judges import clip, judge, judge_model, set_judge_model

CITE_RE = re.compile(r"\[\[source:|SIGNOR-\d+|PMID\s*:?\s*\d+", re.I)
PHOS_RE = re.compile(r"\b(?:de)?phosphorylat\w*", re.I)


def coverage(row: dict, text: str) -> dict:
    """Code reads every source: what share of it went to the reader (skipped sections such as
    references and methods excluded), and how many parts failed."""
    from app.agents.reader import split_parts
    from app.config import get_settings
    from app.vault.store import split_frontmatter
    s = get_settings()
    body = split_frontmatter(text)[1]
    parts = split_parts(body, s.capture_part_chars, s.capture_skip_sections)
    sent = sum(len(p.text) for p in parts)
    failed, summary = 0, None
    for m in row.get("infos") or []:
        hit = re.match(r"Read .+ in \d+ parts: .*?(?:, (\d+) parts skipped)?\.$", m or "")
        if hit:
            summary, failed = m, int(hit.group(1) or 0)
    return {"chars_total": len(body), "chars_sent": sent, "share_read": round(sent / len(body), 3) if body else 0.0,
            "parts": len(parts), "parts_failed": failed, "reading": summary}


def page_stats(vault, names: list[str]) -> list[dict]:
    from app.vault.store import page_links, split_frontmatter, uncited_blocks
    out = []
    for n in names:
        text = vault.read_page(n)
        _, body = split_frontmatter(text)
        links, srcs = page_links(text)
        out.append({"page": n, "words": len(body.split()), "citations": len(CITE_RE.findall(body)),
                    "page_links": len(links), "uncited_blocks": len(uncited_blocks(body))})
    return out


def relation_sentences(text: str) -> list[dict]:
    """Recall proxy: sentences with a (de)phosphorylation verb and two graph proteins."""
    from app.knowledge.entities import extract_mentions
    out = []
    for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text)):
        if PHOS_RE.search(s) and len(s) < 600:
            ents = {m.name or m.text for m in extract_mentions(s, limit=10)}
            if len(ents) >= 2:
                out.append({"sentence": s, "proteins": sorted(ents)})
    return out


def context_around(text: str, quote: str, n: int = 500) -> str:
    flat, q = re.sub(r"\s+", " ", text), re.sub(r"\s+", " ", quote)
    i = flat.lower().find(q.lower()[:80])
    return flat[max(0, i - n): i + len(q) + n] if i >= 0 else quote


async def score_paper(vault, row: dict, use_judge: bool = True) -> dict:
    from app.knowledge.claims import list_claims
    source = row["source"]
    src_text = vault.source_path(source).read_text(encoding="utf-8", errors="replace")
    pages = sorted(set(row["pages_created"]) | set(row["pages_updated"]))
    out = {"coverage": coverage(row, src_text), "pages": page_stats(vault, pages)}
    wiki = "\n\n".join(f"=== PAGE {p} ===\n{vault.read_page(p)}" for p in pages)
    if not use_judge:
        out["claims"] = _claims_rows(vault, source)
        cands = relation_sentences(src_text)
        claimed = {(c["enzyme"].upper(), c["target"].upper()) for c in out["claims"]}
        out["recall_proxy"] = {"candidate_sentences": len(cands), "covered_by_claims": sum(
            1 for s in cands if any(e in map(str.upper, s["proteins"]) and t in map(str.upper, s["proteins"])
                                    for e, t in claimed))}
        return out

    g = await judge("grounding", f"=== SOURCE ===\n{clip(src_text)}\n\n{wiki}")
    out["grounding"] = {"statements_checked": g.statements_checked,
                        "unsupported": [u.model_dump() for u in g.unsupported]}
    f = await judge("findings", f"=== PAPER ===\n{clip(src_text, 60_000)}\n\n=== WIKI ===\n{wiki}")
    out["findings"] = [x.model_dump() for x in f.findings]

    claims = [c for c in list_claims(vault) if c.get("source") == source]
    out["claims"] = [{k: c.get(k) for k in ("id", "enzyme", "target", "site", "mechanism", "effect",
                                            "status", "quote", "review")} for c in claims]
    if claims:
        prompt = "\n\n".join(
            f"CLAIM {c['id']}: {c['enzyme']} {c.get('mechanism')} {c['target']}"
            f"{' at ' + c['site'] if c.get('site') else ''} (effect: {c.get('effect')})\n"
            f"QUOTE: {c['quote']}\nCONTEXT: {context_around(src_text, c['quote'])}" for c in claims)
        labels = await judge("claims", prompt)
        by_id = {x.id: x for x in labels.labels}
        for c in out["claims"]:
            lab = by_id.get(c["id"])
            c["judge"] = lab.label if lab else None
            c["judge_note"] = lab.note if lab else ""
    cands = relation_sentences(src_text)
    claimed = {(c["enzyme"].upper(), c["target"].upper()) for c in claims}
    hit = [s for s in cands if any(e.upper() in map(str.upper, s["proteins"]) and
                                   t.upper() in map(str.upper, s["proteins"]) for e, t in claimed)]
    out["recall_proxy"] = {"candidate_sentences": len(cands), "covered_by_claims": len(hit),
                           "examples_missed": [s["sentence"][:300] for s in cands if s not in hit][:8]}
    return out


def _claims_rows(vault, source: str) -> list[dict]:
    from app.knowledge.claims import list_claims
    return [{k: c.get(k) for k in ("id", "enzyme", "target", "site", "mechanism", "effect", "status", "quote", "review")}
            for c in list_claims(vault) if c.get("source") == source]


async def main_async(vault_name: str, tier: str = "judge") -> None:
    from app.api.deps import registry
    from app.config import get_settings
    get_settings().topic_guard = False
    cap = load(RESULTS / f"capture-{vault_name}.json")
    who = "code" if tier == "code" else slug(judge_model())
    path = RESULTS / f"score-capture-{vault_name}-{who}.json"
    scores = load(path, {"vault": vault_name, "judge": None if tier == "code" else judge_model(), "papers": {}})
    vault = registry().get(vault_name)
    for name, row in cap["papers"].items():
        if row.get("status") != "succeeded" or name in scores["papers"]:
            continue
        print(f"scoring {name} ...", flush=True)
        try:
            scores["papers"][name] = await score_paper(vault, row, use_judge=tier != "code")
        except Exception as e:  # noqa: BLE001 - not saved, so the next run tries this paper again
            print(f"  judge failed ({type(e).__name__}); {name} skipped for now", flush=True)
            continue
        save(path, scores)
        s = scores["papers"][name]
        g = s.get("grounding")
        print(f"  reader got {s['coverage']['share_read']:.0%} of the source; {len(s['pages'])} pages; "
              + (f"{len(g['unsupported'])}/{g['statements_checked']} unsupported; " if g else "")
              + f"{len(s['claims'])} claims", flush=True)
    scores["lint"] = {k: v for k, v in vault.lint_mechanical().items() if v}
    save(path, scores)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--tier", choices=["code", "judge"], default="judge",
                    help="code: mechanical checks only (free); judge: plus the LLM judge")
    ap.add_argument("--judge-model", help="default: JUDGE_MODEL env var, else compat:cliproxy:claude-sonnet-4-6")
    add_groq_key_arg(ap)
    a = ap.parse_args()
    use_groq_key(a.groq_key)
    set_judge_model(a.judge_model)
    asyncio.run(main_async(a.vault, a.tier))


if __name__ == "__main__":
    main()
