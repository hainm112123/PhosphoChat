"""Compare two scored runs: did a change help, and where did anything get worse?

    cd backend && .venv/Scripts/python -m eval.compare --base <score file or run id> --new <...> [--judge <slug>]

Takes two capture score files or two chat score files (`results/score-*.json`), or run ids from
`results/runs.jsonl` (the score file is then found from the run's result file and `--judge`,
default the routine judge). Prints the mean of each metric, the change, and every paper or
question whose metric got worse. A difference of one statement or one question is within the
noise of a single run: rerun before concluding.
"""

from __future__ import annotations

import argparse
import json
import statistics as st

from eval.common import RESULTS, load, slug

ROUTINE_JUDGE = "compat:cliproxy:claude-sonnet-4-6"

# (name, higher is better)
CAPTURE_METRICS = [("unsupported_rate", False), ("findings_covered", True), ("uncited_blocks", False),
                   ("pages", True), ("claims", True), ("claims_supported", True), ("requests", False),
                   ("input_tokens", False), ("seconds", False)]
CHAT_METRICS = [("correctness", True), ("completeness", True), ("groundedness", True), ("citations", True),
                ("invalid_citations", False), ("format_slips", False), ("db_precision", True), ("db_top5_recall", True),
                ("refusal_ok", True), ("input_tokens", False), ("seconds", False)]


def _ledger() -> dict[str, dict]:
    path = RESULTS / "runs.jsonl"
    if not path.is_file():
        return {}
    return {r["run"]: r for r in (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip())}


def resolve(ref: str, judge: str) -> str:
    """A score file name, or a run id -> its score file."""
    if (RESULTS / ref).is_file():
        return ref
    run = _ledger().get(ref)
    if run is None:
        raise SystemExit(f"No score file or run '{ref}' in {RESULTS}")
    who = "code" if judge == "code" else slug(judge)
    stem = run["result"].removesuffix(".json")
    name = (f"score-capture-{run['vault']}-{who}.json" if run["kind"] == "capture" else f"score-{stem}-{who}.json")
    if not (RESULTS / name).is_file():
        raise SystemExit(f"Run {ref} has no score file {name}; score it first.")
    return name


def capture_items(score: dict) -> dict[str, dict]:
    cap = load(RESULTS / f"capture-{score['vault']}.json", {"papers": {}})
    out = {}
    for paper, s in score["papers"].items():
        g, f, row = s.get("grounding"), s.get("findings"), cap["papers"].get(paper, {})
        claims = s.get("claims") or []
        out[paper] = {
            "unsupported_rate": round(len(g["unsupported"]) / g["statements_checked"], 3)
            if g and g.get("statements_checked") else None,
            "findings_covered": round(sum({"yes": 1, "partial": .5}.get(x["in_wiki"], 0) for x in f) / len(f), 2)
            if f else None,
            "uncited_blocks": sum(p["uncited_blocks"] for p in s.get("pages", [])),
            "pages": len(s.get("pages", [])),
            "claims": len(claims),
            "claims_supported": sum(1 for c in claims if c.get("judge") == "supported") if any(
                "judge" in c for c in claims) else None,
            "requests": row.get("requests"), "input_tokens": row.get("input_tokens"), "seconds": row.get("seconds"),
        }
    return out


def chat_items(score: dict) -> dict[str, dict]:
    out = {}
    for qid, s in score["answers"].items():
        j = s.get("judge") or {}
        cit, db = s.get("citations") or {}, s.get("database") or {}
        out[qid] = {
            **{k: j.get(k) for k in ("correctness", "completeness", "groundedness", "citations")},
            "invalid_citations": len(cit.get("invalid", [])) if cit else None,
            "format_slips": cit.get("format_slips") if cit else None,
            "db_precision": db.get("precision"), "db_top5_recall": db.get("top5_recall"),
            "refusal_ok": (1 if s.get("pass") else 0) if s.get("type") == "offtopic" else None,
            "input_tokens": (s.get("usage") or {}).get("input_tokens"), "seconds": s.get("seconds"),
        }
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(st.mean(xs), 3) if xs else None


def compare(base_file: str, new_file: str) -> str:
    base, new = load(RESULTS / base_file), load(RESULTS / new_file)
    kind = "capture" if "papers" in base else "chat"
    items, metrics = (capture_items, CAPTURE_METRICS) if kind == "capture" else (chat_items, CHAT_METRICS)
    b, n = items(base), items(new)
    shared = sorted(set(b) & set(n))
    lines = [f"# {kind}: {base_file}  ->  {new_file}",
             f"judges: {base.get('judge')} -> {new.get('judge')}; items compared: {len(shared)}"
             + (f" (only in base: {sorted(set(b) - set(n))}, only in new: {sorted(set(n) - set(b))})"
                if set(b) ^ set(n) else ""), "",
             "| metric | base | new | change |", "|---|---|---|---|"]
    worse = []
    for name, higher in metrics:
        mb, mn = _mean(b[i][name] for i in shared), _mean(n[i][name] for i in shared)
        delta = None if mb is None or mn is None else round(mn - mb, 3)
        mark = "" if not delta else ("better" if (delta > 0) == higher else "WORSE")
        lines.append(f"| {name} | {mb} | {mn} | {delta if delta is not None else ''} {mark} |")
        if name in ("requests", "input_tokens", "seconds"):
            continue  # cost and speed: reported, not flagged per item
        for i in shared:
            x, y = b[i][name], n[i][name]
            if x is not None and y is not None and (y < x if higher else y > x):
                worse.append(f"- {i}: {name} {x} -> {y}")
    lines += ["", "Got worse:" if worse else "Nothing got worse.", *worse]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--judge", default=ROUTINE_JUDGE, help="judge model of the score files, or 'code'")
    a = ap.parse_args()
    print(compare(resolve(a.base, a.judge), resolve(a.new, a.judge)))


if __name__ == "__main__":
    main()
