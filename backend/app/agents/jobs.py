"""Skill-driven write jobs: capture, sync, lint, digest.

Each job is: deterministic pre-step (in code) -> agent run with AGENTS.md + SKILL.md as
instructions and the write tools -> deterministic post-step (index safety net, log entry,
git commit). The model only does the editorial work; bookkeeping it could get wrong
(source files, log placement, unprocessed-source detection, mechanical lint) is code.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from app.agents.runtime import stream_run
from app.config import get_settings
from app.graph.signor import INSTRUCTIONS as GRAPH_INSTRUCTIONS
from app.tools.agent_tools import Deps, graph_toolset, vault_read_toolset, vault_write_toolset
from app.vault.store import Vault, page_links

JobKind = Literal["capture", "sync", "lint", "digest"]
Emit = Callable[[dict], Awaitable[None]]


class JobReport(BaseModel):
    report: str = Field(description="Markdown summary for the user.")
    log_lines: list[str] = Field(default_factory=list,
                                 description="Extra bullet lines for the log.md entry, as the skill says.")


JOB_RULES = """\
# Running as a background job
No user can answer questions during this run: make reasonable decisions and note open questions
in your report. Work only through your tools. When the work is done, return the final report.
"""


def _instructions(ctx: RunContext[JobContext]) -> str:
    v = ctx.deps.vault
    schema = (v.root / "AGENTS.md").read_text(encoding="utf-8")
    skill = (v.root / ".agents" / "skills" / ctx.deps.kind / "SKILL.md").read_text(encoding="utf-8")
    return f"{schema}\n\n{GRAPH_INSTRUCTIONS}\n\n{JOB_RULES}\n\n# Skill: {ctx.deps.kind}\n\n{skill}"


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

def finish(vault: Vault, kind: JobKind, deps: JobContext, ctx: dict, report: JobReport) -> dict:
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
        lines += [f"Pages created: {fmt(deps.created)}", f"Pages updated: {fmt(deps.updated)}"]
    lines += report.log_lines
    if kind == "lint" and ctx.get("auto_fixed"):
        lines.append("Auto-fixed by PhosphoChat: " + ", ".join(ctx["auto_fixed"]))
    vault.prepend_log(kind, lines)

    subject = {"capture": f"capture: {ctx.get('sources', [''])[0]}",
               "sync": f"sync: {len(ctx.get('sources', []))} source(s)",
               "lint": "lint", "digest": f"digest: {ctx.get('recap', '')}"}[kind]
    sha = vault.commit(subject)
    return {"report": report.report, "pages_created": deps.created,
            "pages_updated": deps.updated, "warnings": warnings, "commit": sha}


# --------------------------------------------------------------------------- run

async def run_job(vault: Vault, kind: JobKind, params: dict, emit: Emit) -> dict:
    """Run one job under the vault's write lock. Rolls the wiki back if the agent fails."""
    async with vault.lock:
        vault.commit(f"source: {params['source']}" if kind == "capture" else "external changes")
        prompt, ctx = prepare(vault, kind, params)
        if prompt is None:
            await emit({"type": "info", "message": ctx["report"]})
            return {"report": ctx["report"], "pages_created": [], "pages_updated": [],
                    "warnings": [], "commit": vault.commit(f"{kind}: prepare")}
        await emit({"type": "info", "message": f"Running {kind} with {get_settings().model}"})
        deps = JobContext(vault=vault, kind=kind)
        report: JobReport | None = None
        try:
            async for ev in stream_run(job_agent(), prompt, deps=deps,
                                       request_limit=get_settings().job_request_limit,
                                       model=params.get("model")):
                if ev["type"] == "result":
                    report = ev["output"]
                    await emit({"type": "usage", **ev["usage"]})
                else:
                    await emit(ev)
        except BaseException:
            vault.rollback()
            raise
        assert report is not None
        return finish(vault, kind, deps, ctx, report)
