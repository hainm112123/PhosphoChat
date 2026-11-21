"""What reaches the chat model's retrieved context, checked without any model call.

    cd backend && .venv/Scripts/python -m eval.context_check --vault eval-proxy-g38

For each question, the context is built exactly as the chat does (knowledge/context.py) and
checked by code:
- paper questions: are the key fact's marker words in the context (MARKERS below)?
- database and claims questions: is the database section there, with its lists?
Run it after any change to the retrieved context: a change that helps one kind of question
can crowd out another (the database section was cut once when wiki excerpts grew).
"""

from __future__ import annotations

import argparse

import yaml

from eval.common import EVAL_DIR

MARKERS = {  # question id -> words that must all appear in the context for its key fact
    "P1": ["benchmarKIN"], "P2": ["CDK12", "PAK1"], "P3": ["EGFR", "SKP1"], "P4": ["50%"],
    "P5": ["CDK1", "KSEA"], "P6": ["PROTsi"], "P7": ["MAPK12"], "P8": ["62 phospho"], "X2": ["ischemi"],
}


def check(vault_name: str, budget: int | None = None) -> dict:
    from app.api.deps import registry
    from app.config import get_settings
    from app.knowledge.context import build_context
    vault = registry().get(vault_name)
    budget = budget or get_settings().chat_context_chars
    questions = yaml.safe_load((EVAL_DIR / "questions.yaml").read_text(encoding="utf-8"))
    rows = {}
    for q in questions:
        if q["type"] == "offtopic":
            continue
        text = build_context(vault, q["question"], budget).text
        row = {"chars": len(text)}
        if q["id"] in MARKERS:
            row["fact_in_context"] = all(w.lower() in text.lower() for w in MARKERS[q["id"]])
        if q["type"] in ("database", "claims") or q["id"] == "N1":
            i = text.find("## From the databases")
            row["db_chars"] = len(text) - i if i >= 0 else 0
            row["db_lists"] = text.count("regulated by") + text.count("acts on")
        rows[q["id"]] = row
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--budget", type=int)
    a = ap.parse_args()
    rows = check(a.vault, a.budget)
    facts = [r["fact_in_context"] for r in rows.values() if "fact_in_context" in r]
    dbs = [r for r in rows.values() if "db_chars" in r]
    for qid, r in rows.items():
        print(qid, r)
    print(f"\nkey facts in context: {sum(facts)}/{len(facts)}; database section present: "
          f"{sum(1 for r in dbs if r['db_chars'] and r['db_lists'])}/{len(dbs)} with lists")


if __name__ == "__main__":
    main()
