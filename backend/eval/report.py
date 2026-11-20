"""Combine capture, scoring and chat results into summary.json and Markdown tables.

    cd backend && .venv/Scripts/python -m eval.report > eval/results/tables.md
"""

from __future__ import annotations

import statistics as st

from eval.common import RESULTS, load, save


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(st.mean(xs), 2) if xs else None


def capture_rows() -> list[dict]:
    rows = []
    for f in sorted(RESULTS.glob("capture-*.json")):
        cap = load(f)
        vault = cap["vault"]
        score = load(RESULTS / f"score-capture-{vault}.json", {"papers": {}})
        for paper, r in cap["papers"].items():
            s = score["papers"].get(paper, {})
            claims = s.get("claims", [])
            g = s.get("grounding", {})
            rows.append({
                "run": f.stem.removeprefix("capture-"), "model": cap["job_model"], "paper": paper,
                "status": r["status"], "error": (r.get("error") or "")[:90], "seconds": r.get("seconds"),
                "requests": r.get("requests"), "input_tokens": r.get("input_tokens"),
                "busy_retries": sum("provider is busy" in (i or "") for i in r.get("infos", [])),
                "pages": len(set(r.get("pages_created", [])) | set(r.get("pages_updated", []))),
                "words": sum(p["words"] for p in s.get("pages", [])) or None,
                "uncited": sum(p["uncited_blocks"] for p in s.get("pages", [])) if s else None,
                "share_read": (s.get("coverage") or {}).get("share_read"),
                "statements": g.get("statements_checked"), "unsupported": len(g.get("unsupported", [])) if g else None,
                "findings_yes": sum(x["in_wiki"] == "yes" for x in s.get("findings", [])) if s else None,
                "findings_partial": sum(x["in_wiki"] == "partial" for x in s.get("findings", [])) if s else None,
                "claims": len(claims) if s else len(r.get("claims", [])),
                "claims_confirmed": sum(c.get("status") == "confirmed" for c in claims),
                "claims_new": sum(c.get("status") == "new" for c in claims),
                "claims_conflicting": sum(c.get("status") == "conflicting" for c in claims),
                "claims_supported": sum(c.get("judge") == "supported" for c in claims),
                "candidate_sentences": (s.get("recall_proxy") or {}).get("candidate_sentences"),
                "covered_sentences": (s.get("recall_proxy") or {}).get("covered_by_claims"),
            })
    return rows


def chat_rows() -> list[dict]:
    rows = []
    for f in sorted(RESULTS.glob("score-chat-*.json")):
        sc = load(f)
        for qid, a in sc["answers"].items():
            j = a.get("judge") or {}
            c = a.get("citations") or {}
            d = a.get("database") or {}
            rows.append({
                "vault": sc["vault"], "model": sc["chat_model"], "q": qid, "type": a["type"],
                "seconds": a["seconds"], "error": bool(a["error"]), "resets": a["resets"], "refused": a["refused"],
                "pass": a.get("pass"), "three_part": a.get("three_part"),
                **{k: j.get(k) for k in ("correctness", "completeness", "groundedness", "citations")},
                "total": sum(j.get(k, 0) for k in ("correctness", "completeness", "groundedness", "citations")) if j else None,
                "cites": c.get("pages", 0) + c.get("sources", 0) + c.get("signor_ids", 0) + c.get("pmids", 0) if c else None,
                "cites_invalid": len(c.get("invalid", [])) if c else None,
                "precision": d.get("precision"), "top5_recall": d.get("top5_recall"), "site_recall": d.get("site_recall"),
            })
    return rows


def table(rows: list[dict], cols: list[str]) -> str:
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + " |")
    return "\n".join(out)


def main() -> None:
    caps, chats = capture_rows(), chat_rows()
    by_model = {}
    for r in chats:
        by_model.setdefault((r["vault"], r["model"]), []).append(r)
    chat_summary = []
    for (vault, model), rs in by_model.items():
        graded = [r for r in rs if r["total"] is not None]
        chat_summary.append({
            "vault": vault, "model": model, "questions": len(rs),
            "errors": sum(r["error"] for r in rs), "resets": sum(r["resets"] for r in rs),
            "mean_total_of_8": _mean([r["total"] for r in graded]),
            "correctness": _mean([r["correctness"] for r in graded]),
            "completeness": _mean([r["completeness"] for r in graded]),
            "groundedness": _mean([r["groundedness"] for r in graded]),
            "citations": _mean([r["citations"] for r in graded]),
            "invalid_citations": sum(r["cites_invalid"] or 0 for r in rs),
            "db_precision": _mean([r["precision"] for r in rs]), "db_top5_recall": _mean([r["top5_recall"] for r in rs]),
            "median_seconds": st.median([r["seconds"] for r in rs]) if rs else None,
            "offtopic_refused": all(r["refused"] for r in rs if r["type"] == "offtopic"),
        })
    save(RESULTS / "summary.json", {"capture": caps, "chat": chats, "chat_summary": chat_summary,
                                    "corpus": load(RESULTS / "corpus.json"), "duplicates": load(RESULTS / "duplicates.json")})
    print("## Capture\n")
    print(table(caps, ["model", "paper", "status", "seconds", "requests", "input_tokens", "busy_retries", "pages",
                       "words", "uncited", "share_read", "statements", "unsupported", "findings_yes", "findings_partial",
                       "claims", "claims_confirmed", "claims_new", "claims_supported", "candidate_sentences",
                       "covered_sentences", "error"]))
    print("\n## Chat summary\n")
    print(table(chat_summary, list(chat_summary[0].keys()) if chat_summary else ["model"]))
    print("\n## Chat per question\n")
    print(table(chats, ["model", "vault", "q", "type", "seconds", "resets", "error", "refused", "three_part", "correctness",
                        "completeness", "groundedness", "citations", "total", "cites", "cites_invalid", "precision",
                        "top5_recall", "site_recall"]))


if __name__ == "__main__":
    main()
