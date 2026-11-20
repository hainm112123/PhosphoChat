"""Score chat answers: checks in code plus a blind judge rubric.

    cd backend && .venv/Scripts/python -m eval.score_chat --file chat-eval-gemini35flash-gpt-oss-20b.json

Code checks: cited pages/sources exist in the vault; cited SIGNOR ids and PMIDs exist in the
graph (PMIDs may also come from the vault's sources); three-part structure; for database
questions, precision of the kinases named against the graph and recall of the best-supported
ones; off-topic refusal. Judge: correctness, completeness, groundedness, citations (0-2).
Writes results/score-<file>. Resumable per question.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

import yaml

from eval.common import EVAL_DIR, RESULTS, add_groq_key_arg, load, save, use_groq_key
from eval.judges import JUDGE_MODEL, judge

PAGE_RE = re.compile(r"\[\[(?!source:)([^\]|#]+)(?:[|#][^\]]*)?\]\]")
SOURCE_RE = re.compile(r"\[\[source:\s*([^\]]+?)\s*\]\]")
SIGNOR_RE = re.compile(r"SIGNOR-\d+")
PMID_RE = re.compile(r"PMID[:\s]*(\d{5,9})", re.I)
SITE_RE = re.compile(r"\b(?:(Ser|Thr|Tyr)|([STY]))[- ]?(\d{2,4})\b")
RESIDUE = {"S": "Ser", "T": "Thr", "Y": "Tyr"}


def gold(spec: dict) -> dict:
    from app.graph import signor
    if spec["kind"] == "regulators":
        rows = signor._run("""MATCH (k:Entity)-[m:MODIFIES {mechanism: 'phosphorylation'}]->(t:Entity {name: $n})
                              RETURN k.name AS name, m.n_sources AS s""", n=spec["protein"])
    elif spec["kind"] == "targets":
        rows = signor._run("""MATCH (k:Entity {name: $n})-[m:MODIFIES {mechanism: 'phosphorylation'}]->(t:Entity)
                              RETURN t.name AS name, m.n_sources AS s""", n=spec["protein"])
    else:
        rows = signor._run("""MATCH (k:Entity {name: $e})-[m:MODIFIES]->(t:Entity {name: $t}) RETURN m.sites AS sites""",
                           e=spec["enzyme"], t=spec["target"])
        sites = sorted({s.split("_ph")[-1] for r in rows for s in (r["sites"] or [])})
        return {"sites": sites}
    ranked = sorted(rows, key=lambda r: (-(r["s"] or 0), r["name"]))
    return {"all": sorted({r["name"] for r in rows}), "top": [r["name"] for r in ranked[:5]]}


def citation_checks(answer: str, vault) -> dict:
    from app.graph import signor
    pages = {p.strip().lower() for p in PAGE_RE.findall(answer)}
    sources = {s.strip() for s in SOURCE_RE.findall(answer)}
    known_pages, known_sources = set(vault.page_names()), {p.name for p in vault.sources_dir.iterdir()}
    sids, pmids = set(SIGNOR_RE.findall(answer)), set(PMID_RE.findall(answer))
    ok_sids = {s for s in sids if signor._run(
        "MATCH ()-[m:MODIFIES]->() WHERE $s IN m.signor_ids RETURN 1 AS x LIMIT 1", s=s)}
    vault_text = " ".join(p.read_text(encoding="utf-8", errors="replace") for p in vault.sources_dir.glob("*.md"))
    ok_pmids = {p for p in pmids if p in vault_text or signor._run(
        "MATCH ()-[m:MODIFIES]->() WHERE $p IN m.pmids RETURN 1 AS x LIMIT 1", p=p)}
    return {"pages": len(pages), "pages_valid": len(pages & known_pages),
            "sources": len(sources), "sources_valid": len(sources & known_sources),
            "signor_ids": len(sids), "signor_valid": len(ok_sids), "pmids": len(pmids), "pmids_valid": len(ok_pmids),
            "invalid": sorted((pages - known_pages) | (sources - known_sources) | (sids - ok_sids) | (pmids - ok_pmids))}


def database_checks(answer: str, q: dict, g: dict) -> dict:
    from app.knowledge.entities import extract_mentions
    if "sites" in g:
        named = {f"{RESIDUE.get(b, a or '')}{n}" for a, b, n in SITE_RE.findall(answer)}
        hit = [s for s in g["sites"] if s in named]
        return {"gold_sites": g["sites"], "sites_named": len(hit), "site_recall": round(len(hit) / len(g["sites"]), 2)}
    asked = q["gold"]["protein"].upper()
    named = {(m.name or m.text).upper() for m in extract_mentions(answer, limit=200)} - {asked}
    gold_all = {x.upper() for x in g["all"]}
    correct = named & gold_all
    return {"named": len(named), "correct": len(correct), "gold_size": len(gold_all),
            "precision": round(len(correct) / len(named), 2) if named else None,
            "top5_recall": round(len({x.upper() for x in g["top"]} & named) / len(g["top"]), 2),
            "wrong": sorted(named - gold_all)[:15]}


def retrieved_text(row: dict, vault, question: str = "") -> str:
    """Everything the chat model was given: the retrieved context (rebuilt the same way the
    chat does: wiki excerpts, claims and the database records for the proteins asked about),
    the wiki pages it lists, and the tool results."""
    parts = []
    if question and row.get("context"):
        from app.config import get_settings
        from app.knowledge.context import build_context
        try:
            text = build_context(vault, question, get_settings().chat_context_chars).text
            parts.append(f"=== RETRIEVED CONTEXT GIVEN TO THE MODEL (wiki, claims, databases) ===\n{text}")
        except Exception as e:  # noqa: BLE001
            parts.append(f"=== (retrieved context could not be rebuilt: {e}) ===")
    for p in ((row.get("context") or {}).get("pages") or []):
        name = p.get("name") if isinstance(p, dict) else p
        if name in vault.page_names():
            parts.append(f"=== WIKI PAGE {name} ===\n{vault.read_page(name)[:6000]}")
    for t in row.get("tool_results") or []:
        parts.append(f"=== TOOL {t['name']} ===\n{(t.get('content') or '')[:3000]}")
    return "\n\n".join(parts)[:60_000] or "(nothing retrieved)"


async def main_async(fname: str) -> None:
    from app.api.deps import registry
    run = load(RESULTS / fname)
    questions = {q["id"]: q for q in yaml.safe_load(Path(EVAL_DIR / "questions.yaml").read_text(encoding="utf-8"))}
    vault = registry().get(run["vault"])
    path = RESULTS / f"score-{fname}"
    out = load(path, {"file": fname, "chat_model": run["chat_model"], "vault": run["vault"],
                      "judge": JUDGE_MODEL, "answers": {}})
    for qid, row in run["answers"].items():
        if qid in out["answers"]:
            continue
        q, ans = questions[qid], row["answer"]
        s: dict = {"type": q["type"], "seconds": row["seconds"], "error": row["error"], "resets": len(row["resets"]),
                   "refused": bool(row["refused"]), "tools": len(row["tools"]), "usage": row["usage"],
                   "chars": len(ans), "three_part": "from your wiki" in ans.lower() and "from the databases" in ans.lower()}
        if q["type"] == "offtopic":
            s["pass"] = s["refused"]
        elif row["error"] or not ans.strip():
            s["pass"] = False
        else:
            s["citations"] = citation_checks(ans, vault)
            facts = q.get("key_facts") or []
            if q["type"] == "database":
                g = gold(q["gold"])
                s["database"] = database_checks(ans, q, g)
                facts = facts or [f"Kinases reported in the databases include: {', '.join(g.get('top', []))} "
                                  f"(of {len(g.get('all', []))} in total)."]
            grade = await judge("answer", f"QUESTION: {q['question']}\n\nKEY FACTS:\n- " + "\n- ".join(facts)
                                + f"\n\nRETRIEVED CONTEXT:\n{retrieved_text(row, vault, q['question'])}\n\nANSWER:\n{ans}")
            s["judge"] = grade.model_dump()
        out["answers"][qid] = s
        save(path, out)
        print(f"{qid}: {s.get('judge') or s.get('pass')}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    add_groq_key_arg(ap)
    a = ap.parse_args()
    use_groq_key(a.groq_key)
    asyncio.run(main_async(a.file))


if __name__ == "__main__":
    main()
