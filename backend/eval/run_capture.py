"""Capture the pilot papers into an evaluation vault with one job model.

    cd backend && .venv/Scripts/python -m eval.run_capture --job-model google:gemini-2.5-flash \
        --vault eval-gemini-flash [--papers a.pdf b.pdf] [--timeout 2400]

Goes through the real capture endpoint (file upload): topic gate, staging, the job
transaction. Resumable: papers that already succeeded in this vault are skipped.
Writes results/capture-<vault>.json.
"""

from __future__ import annotations

import argparse
import time

from eval.common import DATASET, RESULTS, add_groq_key_arg, app_client, load, save, set_role_model, use_groq_key


def wait(client, job_id: str, timeout: float) -> dict:
    end, shown = time.time() + timeout, 0
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        for ev in job.get("events", [])[shown:]:
            if ev.get("type") == "info":
                print(f"   {time.strftime('%H:%M:%S')} {ev['message']}", flush=True)
        shown = len(job.get("events", []))
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(3)
    job = client.get(f"/api/jobs/{job_id}").json()
    job["timed_out"] = True
    return job


def summarise(job: dict, seconds: float) -> dict:
    events = job.get("events", [])
    usage = [e for e in events if e.get("type") == "usage"]
    calls = [{"name": e["name"], "args": e.get("args")} for e in events if e.get("type") == "tool_call"]
    result = job.get("result") or {}
    return {
        "status": job["status"], "timed_out": job.get("timed_out", False), "error": job.get("error"),
        "seconds": round(seconds, 1), "source": job.get("params", {}).get("source"),
        "requests": sum(u.get("requests", 0) for u in usage) or None,
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage) or None,
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage) or None,
        "tool_calls": calls,
        "tool_results": [{"name": e.get("name"), "retry": e.get("retry"), "content": str(e.get("content"))[:400]}
                         for e in events if e.get("type") == "tool_result"],
        "retries": sum(1 for e in events if e.get("type") == "tool_result" and e.get("retry")),
        "infos": [e.get("message") for e in events if e.get("type") == "info"],
        "pages_created": result.get("pages_created", []), "pages_updated": result.get("pages_updated", []),
        "warnings": result.get("warnings", []), "claims": result.get("claims", []),
        "report": result.get("report"),
    }


def main() -> None:
    import logging
    logging.basicConfig(format="   %(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("phosphochat.history").setLevel(logging.INFO)  # size of every agent request
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-model", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--papers", nargs="*")
    ap.add_argument("--timeout", type=float, default=2400)
    ap.add_argument("--attempts", type=int, default=2, help="tries per paper in this process (readings are reused)")
    add_groq_key_arg(ap)
    a = ap.parse_args()
    use_groq_key(a.groq_key)
    papers = a.papers or load(RESULTS / "pilot_papers.json")
    out_path = RESULTS / f"capture-{a.vault}.json"
    out = load(out_path, {"job_model": a.job_model, "vault": a.vault, "papers": {}})
    set_role_model("job", a.job_model)
    with app_client() as client:
        if a.vault not in [v["name"] for v in client.get("/api/vaults").json()]:
            assert client.post("/api/vaults", json={"name": a.vault}).status_code == 201
        for name in papers:
            if out["papers"].get(name, {}).get("status") == "succeeded":
                print(f"skip {name} (done)")
                continue
            data = (DATASET / name).read_bytes()
            for attempt in range(1, a.attempts + 1):
                if capture_one(client, a, name, data, out, out_path, attempt) == "succeeded":
                    break


def capture_one(client, a, name: str, data: bytes, out: dict, out_path, attempt: int) -> str:
    t = time.time()
    r = client.post(f"/api/vaults/{a.vault}/capture", data={"verify": "false"},
                    files={"file": (name, data, "application/pdf")})
    if r.status_code != 202:
        out["papers"][name] = {"status": "rejected", "http": r.status_code, "error": r.text[:500],
                               "seconds": round(time.time() - t, 1), "attempt": attempt}
        print(f"{name}: rejected {r.status_code} {r.text[:200]}")
        save(out_path, out)
        return "rejected"
    job = wait(client, r.json()["job"]["id"], a.timeout)
    row = {**summarise(job, time.time() - t), "attempt": attempt}
    previous = out["papers"].get(name)
    if previous and previous.get("status") != "succeeded":
        row["earlier_attempts"] = previous.get("earlier_attempts", []) + [
            {k: previous.get(k) for k in ("status", "seconds", "error", "attempt", "infos", "tool_calls")}]
    out["papers"][name] = row
    save(out_path, out)
    print(f"{name} (attempt {attempt}): {row['status']} in {row['seconds']} s, {len(row['pages_created'])} new / "
          f"{len(row['pages_updated'])} updated pages, {len(row['claims'])} claims, "
          f"{row['requests']} requests, {row['input_tokens']} in-tokens"
          + (f"\n   error: {row['error'][:300]}" if row["error"] else ""), flush=True)
    return row["status"]


if __name__ == "__main__":
    main()
