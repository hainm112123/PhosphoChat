"""Ask the pilot questions in an evaluation vault with one chat model.

    cd backend && .venv/Scripts/python -m eval.run_chat --chat-model groq:openai/gpt-oss-20b \
        --vault eval-gemini35flash [--only P1 P2]

Each question is asked in a fresh conversation through the real chat endpoint (SSE): topic
gate, pre-fetched context, tools, recovery paths. Resumable. Writes
results/chat-<vault>-<model>.json.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import yaml

from eval.common import (EVAL_DIR, RESULTS, add_groq_key_arg, app_client, load, save, set_role_model, slug,
                         sse_events, use_groq_key, record_run)


def ask(client, vault: str, question: str) -> dict:
    t = time.time()
    with client.stream("POST", f"/api/vaults/{vault}/chat", json={"message": question}) as r:
        raw = r.read().decode("utf-8", errors="replace")
    events = sse_events(raw)
    text, notices = "", []
    for ev, d in events:
        if ev == "text":
            text += d.get("delta", "")
        elif ev == "reset":
            text, _ = "", notices.append(d.get("message"))
    get = lambda name: [d for e, d in events if e == name]  # noqa: E731
    done = get("done")
    return {
        "answer": text, "seconds": round(time.time() - t, 1),
        "refused": (get("off_topic") or [{}])[0].get("message"),
        "error": (get("error") or [{}])[0].get("message"),
        "resets": notices, "context": (get("context") or [None])[0],
        "tools": [d.get("name") for d in get("tool_call")],
        "tool_results": [{"name": d.get("name"), "content": d.get("content")} for d in get("tool_result")],
        "graph_edges": sum(len(d.get("edges", [])) for d in get("graph")),
        "usage": done[0]["usage"] if done else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-model", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--pause", type=float, default=0, help="seconds between questions (rate limits)")
    ap.add_argument("--guard-model", help="topic-check model (default: the chat model)")
    ap.add_argument("--tag", default="", help="suffix for the result file, e.g. it3 (one file per iteration)")
    add_groq_key_arg(ap)
    a = ap.parse_args()
    use_groq_key(a.groq_key)
    questions = yaml.safe_load(Path(EVAL_DIR / "questions.yaml").read_text(encoding="utf-8"))
    path = RESULTS / f"chat-{a.vault}-{slug(a.chat_model)}{'-' + a.tag if a.tag else ''}.json"
    out = load(path, {"chat_model": a.chat_model, "vault": a.vault, "answers": {}})
    set_role_model("chat", a.chat_model)
    from app.config import get_settings
    get_settings().guard_model = a.guard_model or a.chat_model
    with app_client() as client:
        for q in questions:
            if (a.only and q["id"] not in a.only) or (q["id"] in out["answers"] and not out["answers"][q["id"]].get("error")):
                continue
            row = ask(client, a.vault, q["question"])
            out["answers"][q["id"]] = row
            save(path, out)
            status = "REFUSED" if row["refused"] else "ERROR " + (row["error"] or "")[:120] if row["error"] else "ok"
            print(f"{q['id']}: {status} in {row['seconds']} s, {len(row['answer'])} chars, tools {row['tools']}, "
                  f"resets {len(row['resets'])}", flush=True)
            if a.pause:
                time.sleep(a.pause)
    answers = out["answers"].values()
    run = record_run("chat", vault=a.vault, result=path.name, questions=len(out["answers"]),
                     errors=sum(1 for r in answers if r.get("error")),
                     input_tokens=sum((r.get("usage") or {}).get("input_tokens", 0) for r in answers),
                     requests=sum((r.get("usage") or {}).get("requests", 0) for r in answers))
    print(f"run {run} -> {path.name}")


if __name__ == "__main__":
    main()
