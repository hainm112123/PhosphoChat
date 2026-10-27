"""Skill-driven write jobs: capture, sync, lint, digest.

Each job is: deterministic pre-step (in code) -> agent run with the app's AGENTS.md + SKILL.md
as instructions and the write tools -> (capture/sync) fact-check against the sources plus one
revision turn -> deterministic post-step (index safety net, log entry, git commit). The model only does the editorial work; bookkeeping it could get wrong
(source files, log placement, unprocessed-source detection, mechanical lint) is code.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from app import prompts
from app.agents.runtime import JobFailed, explain_error, stream_run
from app.agents.verify import Issue, verify_pages
from app.config import get_settings
from app.llm import model_name
from app.tools.agent_tools import Deps, graph_toolset, vault_read_toolset, vault_write_toolset
from app.vault.store import StagedSource, Vault, page_links

JobKind = Literal["capture", "sync", "lint", "digest"]
Emit = Callable[[dict], Awaitable[None]]


class JobReport(BaseModel):
    report: str = Field(description="Markdown summary for the user.")
    key_takeaway: str | None = Field(None, description="capture/sync: the single most important point.")
    contradictions: list[str] = Field(default_factory=list,
                                      description="capture/sync: pages where a contradiction callout was added.")
    log_lines: list[str] = Field(default_factory=list,
                                 description="lint/digest: extra bullet lines for the log.md entry, as the skill says.")


JOB_RULES = """\
# Running as a background job
No user can answer questions during this run: make reasonable decisions and note open questions
in your report. Work only through your tools. When the work is done, return the final report.
"""


def _instructions(ctx: RunContext[JobContext]) -> str:
    return (f"{prompts.schema()}\n\n{JOB_RULES}\n\n"
            f"# Skill: {ctx.deps.kind}\n\n{prompts.skill(ctx.deps.kind)}")


@dataclass
class JobContext(Deps):
    kind: JobKind = "capture"


@lru_cache
def job_agent() -> Agent[JobContext, JobReport]:
    return Agent(
        deps_type=JobContext,
        output_type=JobReport,
        name="phosphochat-job",
        instructions=_instructions,
        toolsets=[vault_read_toolset(), vault_write_toolset(), graph_toolset()],
        defer_model_check=True,
    )


# --------------------------------------------------------------------------- pre-steps

def _iso_week_name(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-w{w:02d}-recap"


def prepare(vault: Vault, kind: JobKind, params: dict) -> tuple[str | None, dict]:
    """Return (prompt, context for the post-step). prompt=None means nothing to do."""
    if kind == "capture":
        name = params["source"]
        src = next((s for s in vault.list_sources() if s["name"] == name), {})
        return (f"Task: capture.\nThe new source is saved as `{name}` "
                f"(title: {src.get('title')!r}, content_type: {src.get('content_type')}).\n"
                "Follow the capture skill: read it, update or create the affected wiki pages, "
                "index new pages, then return the report.", {"sources": [name]})

    if kind == "sync":
        prepared = vault.prepare_dropped_sources()
        todo = vault.unprocessed_sources()
        if not todo:
            return None, {"report": "Nothing to sync: every source already appears in log.md."}
        batch = todo[:10]
        more = f"\n({len(todo) - len(batch)} more remain for the next sync.)" if len(todo) > 10 else ""
        return ("Task: sync.\nUnprocessed sources (frontmatter already prepared):\n"
                + "".join(f"- {s}\n" for s in batch) + more
                + "\nFollow the sync skill, then return the report.",
                {"sources": batch, "prepared": prepared})

    if kind == "lint":
        findings = vault.lint_mechanical()
        fixed = []
        for n in findings["missing_from_index"]:
            vault.upsert_index(n, vault.page_meta(n).get("summary", ""),
                               "Recaps" if n.endswith("-recap") else "Pages")
            fixed.append(f"indexed [[{n}]]")
        for n in findings["dead_index_entries"]:
            vault.remove_index_entry(n)
            fixed.append(f"removed dead index entry {n}")
        return ("Task: lint.\nMechanical checks already run by PhosphoChat:\n```json\n"
                + json.dumps(findings, indent=1)
                + "\n```\nAlready auto-fixed: " + (", ".join(fixed) or "nothing")
                + "\nFollow the lint skill for the remaining checks, write the lint-report page, "
                  "then return the report.", {"auto_fixed": fixed})

    if kind == "digest":
        end = date.fromisoformat(params["end"]) if params.get("end") else date.today()
        start = end - timedelta(days=int(params.get("days", 7)) - 1)
        entries = [e for e in vault.log_entries()
                   if e.kind != "digest" and start <= _entry_date(e.timestamp) <= end]
        if not entries:
            return None, {"report": f"No log entries between {start} and {end}; no recap written."}
        recap = _iso_week_name(end)
        previous = sorted(n for n in vault.page_names() if n.endswith("-recap") and n < recap)
        return (f"Task: digest.\nPeriod: {start} to {end}. Recap page name: `{recap}`.\n"
                f"Previous recap: {('`' + previous[-1] + '`') if previous else 'none'}.\n"
                "Log entries in the period:\n"
                + "\n\n".join(f"## {e.timestamp} · {e.kind}\n{e.body}" for e in entries)
                + "\n\nFollow the digest skill, then return the report.",
                {"recap": recap})

    raise ValueError(f"Unknown job kind {kind}")


def _entry_date(ts: str) -> date:
    try:
        return datetime.fromisoformat(ts).date()
    except ValueError:
        return date.min


# --------------------------------------------------------------------------- post-step

def finish(vault: Vault, kind: JobKind, deps: JobContext, ctx: dict, report: JobReport,
           fact_check: dict | None = None) -> dict:
    # Safety net: every created page ends up in the index.
    index = vault.index_entries()
    for n in deps.created:
        if n not in index and n != "lint-report":
            vault.upsert_index(n, vault.page_meta(n).get("summary", ""),
                               "Recaps" if n.endswith("-recap") else "Pages")

    warnings = []
    names = set(vault.page_names())
    for n in deps.touched():
        links, _ = page_links(vault.read_page(n))
        bad = sorted(l for l in links if l not in names)
        if bad:
            warnings.append(f"[[{n}]] links to missing pages: {', '.join(bad)}")

    fmt = lambda xs: ", ".join(f"[[{x}]]" for x in xs) or "none"  # noqa: E731
    lines: list[str] = []
    if kind == "capture":
        lines.append(f"Source: sources/{ctx['sources'][0]}")
    elif kind == "sync":
        lines.append("Sources processed: " + ", ".join(ctx["sources"]))
    if kind in ("capture", "sync"):
        lines += [f"Pages created: {fmt(deps.created)}", f"Pages updated: {fmt(deps.updated)}",
                  f"Contradictions: {fmt([c for c in report.contradictions if c in names])}"]
        if fact_check is not None and fact_check.get("error"):
            lines.append("Fact-check: unavailable (pages kept unverified)")
        elif fact_check is not None:
            lines.append(f"Fact-check: {fact_check['issues_found']} unsupported claim(s) found"
                         + (", sent back for revision" if fact_check["issues_found"] else ""))
        if report.key_takeaway:
            lines.append(f"Key takeaway: {' '.join(report.key_takeaway.split())}")
    else:
        lines += report.log_lines
    if kind == "lint" and ctx.get("auto_fixed"):
        lines.append("Auto-fixed by PhosphoChat: " + ", ".join(ctx["auto_fixed"]))
    vault.prepend_log(kind, lines)

    subject = {"capture": f"capture: {ctx.get('sources', [''])[0]}",
               "sync": f"sync: {len(ctx.get('sources', []))} source(s)",
               "lint": "lint", "digest": f"digest: {ctx.get('recap', '')}"}[kind]
    sha = vault.commit(subject)
    return {"report": report.report, "pages_created": deps.created,
            "pages_updated": deps.updated, "warnings": warnings, "commit": sha,
            "fact_check": fact_check}


# --------------------------------------------------------------------------- run

async def run_job(vault: Vault, kind: JobKind, params: dict, emit: Emit,
                  staged: StagedSource | None = None) -> dict:
    """Run one job under the vault's write lock, as a transaction: either every step succeeds
    and the job ends in one commit, or the vault is put back exactly as it was.

    A capture gets its source as `staged` and writes it only here, inside the transaction, so
    a capture that fails leaves no source behind."""
    async with vault.lock:
        vault.commit("external changes")  # so a rollback can only undo this job's own work
        base = vault.head()
        created_files = []
        deps = JobContext(vault=vault, kind=kind)
        try:
            if kind == "capture":
                if staged is None:
                    raise JobFailed("Nothing to capture: the source is no longer staged. Capture it again.")
                params["source"] = vault.save_staged(staged)
                created_files.append(vault.sources_dir / params["source"])
                if staged.original:
                    created_files.append(vault.sources_dir / (Path(params["source"]).stem
                                                              + Path(staged.original[0]).suffix.lower()))
            prompt, ctx = prepare(vault, kind, params)
            if prompt is None:
                await emit({"type": "info", "message": ctx["report"]})
                return {"report": ctx["report"], "pages_created": [], "pages_updated": [],
                        "warnings": [], "commit": vault.commit(f"{kind}: prepare")}
            model = params.get("model")
            await emit({"type": "info", "message": f"Running {kind} with {model_name(model, 'job')}"})
            report, messages = await _agent_turn(prompt, deps, model, emit)
            if kind in ("capture", "sync") and not deps.touched():
                raise JobFailed("The model wrote no wiki pages, so nothing was saved."
                                + (f" Its report:\n\n{report.report}" if report.report else ""))
            fact_check = None
            verify = params.get("verify", get_settings().verify_captures)
            if kind in ("capture", "sync") and verify:
                fact_check, revised = await _fact_check(vault, ctx["sources"], deps, messages,
                                                        model, emit)
                report = revised or report
            return finish(vault, kind, deps, ctx, report, fact_check)
        except BaseException:
            created_files += [vault.wiki_dir / f"{n}.md" for n in deps.created]
            vault.rollback_to(base, created_files)
            raise


async def _agent_turn(prompt: str, deps: JobContext, model: str | None, emit: Emit,
                      history: list | None = None) -> tuple[JobReport, list]:
    report, messages = None, []
    async for ev in stream_run(job_agent(), prompt, deps=deps, history=history,
                               request_limit=get_settings().job_request_limit, model=model,
                               role="job"):
        if ev["type"] == "result":
            report, messages = ev["output"], ev["messages"]
            await emit({"type": "usage", **ev["usage"]})
        else:
            await emit(ev)
    assert report is not None
    return report, messages


def _issue_lines(issues: list[Issue]) -> str:
    return "\n".join(f"- [[{i.page}]] ({i.problem}): \"{i.claim}\" (source: {i.note})" for i in issues)


async def _fact_check(vault: Vault, sources: list[str], deps: JobContext, messages: list,
                      model: str | None, emit: Emit) -> tuple[dict, JobReport | None]:
    """Verify the touched pages against the job's sources; if anything is unsupported, give
    the writer one revision turn (continuing its own conversation)."""
    pages = deps.touched()
    await emit({"type": "info", "message": f"Fact-checking {len(pages)} page(s) against the source"})
    try:
        issues = await verify_pages(vault, sources, pages, model)
    except Exception as e:
        # All-or-nothing: unverified pages are not kept. Turn VERIFY_CAPTURES off to skip the check.
        raise JobFailed(f"The fact-check failed, so nothing was saved: {explain_error(e)}") from e
    result = {"checked_pages": pages, "issues_found": len(issues),
              "issues": [i.model_dump() for i in issues]}
    await emit({"type": "fact_check", **result})
    if not issues:
        return result, None
    prompt = ("A fact-check of your pages against the source found claims the source does not "
              "support:\n" + _issue_lines(issues) + "\n\nFor each one, remove the claim or rewrite "
              "it to say only what the source states. Use write_page with the whole corrected page; "
              "change nothing else. Then return the final report again.")
    revised, _ = await _agent_turn(prompt, deps, model, emit, history=messages)
    return result, revised
