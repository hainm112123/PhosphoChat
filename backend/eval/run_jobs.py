"""Test lint, digest and sync on a copy of an evaluation vault, with code checks only.

    cd backend && .venv/Scripts/python -m eval.run_jobs --vault eval-proxy-g38 --job-model compat:cliproxy:gemini-3-flash
        [--jobs lint digest sync] [--sync-paper <pdf>]

The vault is copied to `<vault>-jobs` first, so the capture baseline stays untouched. Each job's
checks are mechanical:
- lint: the job succeeds; `lint-report` exists with its frontmatter; its counts match the
  mechanical checks; the log entry has the "Issues found" line; which other pages it changed.
- digest: the recap page exists, is indexed under Recaps and has every section of the skill;
  no uncited blocks and no links to missing pages; the log entry names the recap.
- sync: a dropped PDF gets a note, is processed (named in log.md), and pages are written.
Writes results/jobs-<vault>-<model>.json and logs the run in runs.jsonl.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import time

from eval.common import DATASET, RESULTS, app_client, load, record_run, save, set_role_model, slug
from eval.run_capture import wait

RECAP_SECTIONS = ["What came in", "What changed in the wiki", "Themes and patterns",
                  "Contradictions and tensions", "Open questions"]


def changed_files(vault, before: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(vault.root), "diff", "--name-only", before, "HEAD"],
                         capture_output=True, text=True, check=False).stdout
    return sorted(x for x in out.splitlines() if x)


def run(client, vault_name: str, kind: str, timeout: float) -> dict:
    r = client.post(f"/api/vaults/{vault_name}/jobs/{kind}", json={})
    assert r.status_code == 202, r.text
    t = time.time()
    body = r.json()
    job = wait(client, body.get("job", body)["id"], timeout)
    usage = [e for e in job.get("events", []) if e.get("type") == "usage"]
    return {"status": job["status"], "error": (job.get("error") or "")[:400], "seconds": round(time.time() - t, 1),
            "requests": sum(u.get("requests", 0) for u in usage),
            "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
            "tool_calls": [e["name"] for e in job.get("events", []) if e.get("type") == "tool_call"],
            "result": job.get("result") or {}}


def check_lint(vault, before: str, row: dict) -> dict:
    checks = {"succeeded": row["status"] == "succeeded", "report_exists": vault.page_exists("lint-report")}
    if checks["report_exists"]:
        meta = vault.page_meta("lint-report")
        checks["report_frontmatter"] = str(meta.get("title", "")).lower() == "lint report" and bool(meta.get("summary"))
        body = vault.read_page("lint-report")
        found = vault.lint_mechanical()
        # The counts it reports should match the mechanical checks it was given.
        expected = {"broken links": len(found["broken_links"]), "orphans": len(found["orphans"])}
        checks["counts_match"] = all(re.search(rf"{label}\D{{0,20}}{n}\b|\b{n}\b\D{{0,20}}{label}", body, re.I)
                                     for label, n in expected.items())
        checks["expected_counts"] = expected
    entry = vault.log_entries()[0] if vault.log_entries() else None
    checks["log_line"] = bool(entry and entry.kind == "lint" and "Issues found" in entry.body)
    files = changed_files(vault, before)
    checks["changed_files"] = files
    checks["other_pages_changed"] = [f for f in files if f.startswith("wiki/") and f != "wiki/lint-report.md"]
    return checks


def check_digest(vault, before: str, row: dict) -> dict:
    checks = {"succeeded": row["status"] == "succeeded"}
    recaps = [n for n in vault.page_names() if n.endswith("-recap")]
    checks["recap"] = recaps[-1] if recaps else None
    if recaps:
        name, text = recaps[-1], vault.read_page(recaps[-1])
        checks["indexed_under_recaps"] = name in vault.index_entries() and "## Recaps" in vault.read_index().split(
            f"[[{name}]]")[0]
        checks["missing_sections"] = [s for s in RECAP_SECTIONS if not re.search(rf"^##\s+{s}", text, re.M | re.I)]
        found = vault.lint_mechanical()
        checks["uncited_blocks"] = sum(u["count"] for u in found["uncited_blocks"] if u["page"] == name)
        checks["broken_links"] = [b["link"] for b in found["broken_links"] if b["page"] == name]
    entry = vault.log_entries()[0] if vault.log_entries() else None
    checks["log_line"] = bool(entry and entry.kind == "digest" and "Recap:" in entry.body)
    checks["changed_files"] = changed_files(vault, before)
    return checks


def check_sync(vault, before: str, row: dict, pdf: str) -> dict:
    note = f"{pdf.rsplit('.', 1)[0]}.md"
    log = vault.read_log_text()
    res = row["result"]
    return {"succeeded": row["status"] == "succeeded", "note_created": (vault.sources_dir / note).is_file(),
            "named_in_log": note in log, "pages_written": len(res.get("pages_created", [])) + len(res.get("pages_updated", [])),
            "claims": len(res.get("claims", [])), "changed_files": changed_files(vault, before)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True, help="evaluation vault to copy (left untouched)")
    ap.add_argument("--job-model", required=True)
    ap.add_argument("--jobs", nargs="*", default=["lint", "digest", "sync"])
    ap.add_argument("--sync-paper", default="1-s2.0-S2405471220302428-main.pdf")
    ap.add_argument("--timeout", type=float, default=2400)
    a = ap.parse_args()
    set_role_model("job", a.job_model)
    from app.api.deps import registry
    from app.config import get_settings
    get_settings().topic_guard = False   # the sync paper is on-topic; don't spend a guard call
    reg = registry()
    copy = f"{a.vault}-jobs"
    src_root, dst_root = reg.get(a.vault).root, reg.base / copy
    if dst_root.exists():
        shutil.rmtree(dst_root, onerror=lambda f, p, _: (__import__("os").chmod(p, 0o700), f(p)))
    shutil.copytree(src_root, dst_root)
    path = RESULTS / f"jobs-{a.vault}-{slug(a.job_model)}.json"
    out = {"vault": copy, "job_model": a.job_model, "jobs": {}}
    with app_client() as client:
        vault = reg.get(copy)
        for kind in a.jobs:
            before = vault.head()
            if kind == "sync":
                shutil.copy2(DATASET / a.sync_paper, vault.sources_dir / a.sync_paper)
            print(f"== {kind}", flush=True)
            row = run(client, copy, kind, a.timeout)
            check = {"lint": check_lint, "digest": check_digest}.get(kind)
            row["checks"] = check(vault, before, row) if check else check_sync(vault, before, row, a.sync_paper)
            out["jobs"][kind] = row
            save(path, out)
            print(f"   {row['status']} in {row['seconds']} s, {row['requests']} requests; checks: "
                  f"{ {k: v for k, v in row['checks'].items() if k != 'changed_files'} }", flush=True)
    run_id = record_run("jobs", vault=copy, result=path.name, jobs={k: v["status"] for k, v in out["jobs"].items()})
    print(f"run {run_id} -> {path.name}")


if __name__ == "__main__":
    main()
