"""Skill-driven write jobs: capture, sync, lint, digest.

Each job is: deterministic pre-step (in code) -> (capture/sync) code reads the sources in parts
and records their claims -> agent run with AGENTS.md + the kind's SKILL.md as instructions and
only the kind's tools -> (capture/sync) optional fact-check plus one revision turn ->
deterministic post-step (index safety net, log entry, git commit). The model only does the
editorial work; bookkeeping it could get wrong (reading, claims, source files, log placement,
unprocessed-source detection, mechanical lint) is code.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import ProcessHistory

from app import prompts
from app.agents.reader import compact_history, read_in_parts
from app.agents.runtime import JobFailed, explain_error, is_bad_tool_call, is_too_large, is_transient, stream_run
from app.agents.verify import Issue, verify_pages
from app.config import get_settings
from app.knowledge import topic
from app.llm import model_name
from app.tools.agent_tools import Deps, job_tool_names, job_toolsets
from app.vault.store import StagedSource, Vault, page_links, split_frontmatter

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
    return f"{prompts.schema()}\n{JOB_RULES}\n{prompts.skill(ctx.deps.kind)}"


@dataclass
class JobContext(Deps):
    kind: JobKind = "capture"


@lru_cache
def job_agent() -> Agent[JobContext, JobReport]:
    """One agent for every job kind; each run gets its kind's skill and only its kind's tools."""
    return Agent(
        deps_type=JobContext,
        output_type=JobReport,
        name="phosphochat-job",
        instructions=_instructions,
        toolsets=job_toolsets(),
        capabilities=[ProcessHistory(compact_history)],
        defer_model_check=True,
    )


# --------------------------------------------------------------------------- pre-steps

def _iso_week_name(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-w{w:02d}-recap"


INDEX_IN_PROMPT_CHARS = 3000


def _index_section(vault: Vault) -> str:
    """The index, in the task itself (saves the writer a tool round trip), unless it is large."""
    index = vault.read_index().strip()
    if len(index) > INDEX_IN_PROMPT_CHARS:
        return "\n\nThe index is large: call read_index to see the existing pages."
    if not vault.index_entries():
        return "\n\nThe wiki has no pages yet."
    return f"\n\nCurrent index.md:\n{index}"


def prepare(vault: Vault, kind: JobKind, params: dict,
            skip: dict[str, str] | None = None) -> tuple[str | None, dict]:
    """Return (prompt, context for the post-step). prompt=None means nothing to do.
    `skip`: sync sources judged off-topic ({name: reason}), left unprocessed."""
    skip = skip or {}
    if kind == "capture":
        name = params["source"]
        src = next((s for s in vault.list_sources() if s["name"] == name), {})
        return (f"Task: capture.\nThe new source is saved as `{name}` "
                f"(title: {src.get('title')!r}, content_type: {src.get('content_type')}).\n"
                "Write the wiki pages for it from the reading notes below, then return the report."
                + _index_section(vault), {"sources": [name]})

    if kind == "sync":
        prepared = vault.prepare_dropped_sources()
        todo = [s for s in vault.unprocessed_sources() if s not in skip]
        if not todo:
            report = "Nothing to sync: every source already appears in log.md."
            if skip:
                report = f"Nothing to sync: the {len(skip)} new source(s) look off-topic."
            return None, {"report": report, "skipped": skip}
        batch = todo[:10]
        more = f"\n({len(todo) - len(batch)} more remain for the next sync.)" if len(todo) > 10 else ""
        return ("Task: sync.\nUnprocessed sources (frontmatter already prepared):\n"
                + "".join(f"- {s}\n" for s in batch) + more
                + "\nWrite the wiki pages for them from the reading notes below, then return the report."
                + _index_section(vault), {"sources": batch, "prepared": prepared, "skipped": skip})

    if kind == "lint":
        findings = vault.lint_mechanical()
        from app.knowledge.claims import list_claims
        brief = lambda c: {k: c.get(k) for k in ("enzyme", "target", "site", "source", "page", "detail")}  # noqa: E731
        claims = list_claims(vault)
        findings["claims_new"] = [brief(c) for c in claims if c["status"] == "new"]
        findings["claims_conflicting"] = [brief(c) for c in claims if c["status"] == "conflicting"]
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

    warnings = _off_topic_warnings(ctx.get("skipped"))
    names = set(vault.page_names())
    for n in deps.touched():
        page = vault.read_page(n)
        links, _ = page_links(page)
        bad = sorted(l for l in links if l not in names)
        if bad and kind in ("capture", "sync"):
            # Links to pages nobody wrote become plain text, so the model needn't rewrite a page
            # just to remove them (on small budgets those rewrites are what a job spends).
            page = _unlink(page, set(bad))
            vault.write_page(n, page)
            warnings.append(f"[[{n}]]: links to pages not written turned into plain text: {', '.join(bad)}")
        elif bad:
            warnings.append(f"[[{n}]] links to missing pages: {', '.join(bad)}")
        if n != "lint-report" and not topic.has_topic_terms(split_frontmatter(page)[1]):
            warnings.append(f"[[{n}]] names no protein, drug, disease or kinase term; check it belongs here.")

    fmt = lambda xs: ", ".join(f"[[{x}]]" for x in xs) or "none"  # noqa: E731
    lines: list[str] = []
    if kind == "capture":
        lines.append(f"Source: sources/{ctx['sources'][0]}")
    elif kind == "sync":
        lines.append("Sources processed: " + ", ".join(ctx["sources"]))
        if ctx.get("skipped"):  # not named, so the next sync looks at them again
            lines.append(f"Skipped as off-topic: {len(ctx['skipped'])} source(s)")
    if kind in ("capture", "sync"):
        lines += [f"Pages created: {fmt(deps.created)}", f"Pages updated: {fmt(deps.updated)}",
                  f"Contradictions: {fmt([c for c in report.contradictions if c in names])}"]
        if fact_check is not None and fact_check.get("error"):
            lines.append("Fact-check: unavailable (pages kept unverified)")
        elif fact_check is not None:
            lines.append(f"Fact-check: {fact_check['issues_found']} unsupported claim(s) found"
                         + (", sent back for revision" if fact_check["issues_found"] else ""))
        if deps.claims:
            new = sum(1 for c in deps.claims if c["status"] == "new")
            conflicting = sum(1 for c in deps.claims if c["status"] == "conflicting")
            lines.append(f"Relations recorded: {len(deps.claims)} (new: {new}, conflicting: {conflicting})")
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
    if deps.claims:  # show the new claims in the graph (best effort; relations.jsonl is the truth)
        from app.knowledge.claims import safe_sync
        safe_sync(vault)
    return {"report": report.report, "pages_created": deps.created,
            "pages_updated": deps.updated, "warnings": warnings, "commit": sha,
            "fact_check": fact_check,
            "claims": [{k: c.get(k) for k in ("id", "enzyme", "target", "site", "status", "detail")}
                       for c in deps.claims]}


def _unlink(page: str, names: set[str]) -> str:
    """`[[name]]` / `[[name|label]]` -> `name` / `label` for the given page names."""
    def plain(m: re.Match) -> str:
        target = m.group(1).strip().removesuffix(".md")
        if target not in names:
            return m.group(0)
        label = m.group(2)
        return (label or target).strip()
    return re.sub(r"\[\[([^\]|#]+)(?:[#][^\]|]*)?(?:\|([^\]]*))?\]\]", plain, page)


def _off_topic_warnings(skipped: dict[str, str] | None) -> list[str]:
    return [f"sources/{name} looks off-topic ({reason or 'outside the scope'}), so it wasn't "
            "processed. To include it anyway, move it out of sources/ and capture it with "
            "'Capture anyway'." for name, reason in (skipped or {}).items()]


async def _off_topic_sources(vault: Vault, emit: Emit, batch: int = 10) -> dict[str, str]:
    """Sync: the new sources the topic check rejects, checked in order until a batch of
    on-topic ones is found (the rest wait for the next sync)."""
    if not get_settings().topic_guard:
        return {}
    vault.prepare_dropped_sources()  # notes for dropped files, so their text can be read
    skipped, accepted = {}, 0
    for name in vault.unprocessed_sources():
        if accepted >= batch:
            break
        meta, body = split_frontmatter((vault.sources_dir / name).read_text(encoding="utf-8", errors="replace"))
        verdict = await topic.check_source(str(meta.get("title") or name), body)
        if verdict.on_topic:
            accepted += 1
            if verdict.by == "error":
                await emit({"type": "info", "message": f"Topic check unavailable for {name}; processing it unchecked."})
        else:
            skipped[name] = verdict.reason
            await emit({"type": "info", "message": f"Skipping {name}: looks off-topic ({verdict.reason})."})
    return skipped


# --------------------------------------------------------------------------- run

async def run_job(vault: Vault, kind: JobKind, params: dict, emit: Emit,
                  staged: StagedSource | None = None) -> dict:
    """Run one job under the vault's write lock, as a transaction: either every step succeeds
    and the job ends in one commit, or the vault is put back exactly as it was.

    A capture gets its source as `staged` and writes it only here, inside the transaction, so
    a capture that fails leaves no source behind."""
    async with vault.lock:
        vault.recover_interrupted_job()   # leftovers of a job killed mid-way are not "external"
        vault.commit("external changes")  # so a rollback can only undo this job's own work
        base = vault.head()
        vault.begin_job(base)
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
            if kind == "capture" and params.get("topic_note"):
                await emit({"type": "info", "message": params["topic_note"]})
            skip = await _off_topic_sources(vault, emit) if kind == "sync" else None
            prompt, ctx = prepare(vault, kind, params, skip)
            if prompt is None:
                await emit({"type": "info", "message": ctx["report"]})
                return {"report": ctx["report"], "pages_created": [], "pages_updated": [],
                        "warnings": _off_topic_warnings(ctx.get("skipped")),
                        "commit": vault.commit(f"{kind}: prepare")}
            model = params.get("model")
            await emit({"type": "info", "message": f"Running {kind} with {model_name(model, 'job')}"})
            if kind in ("capture", "sync"):
                report, messages = await _write_from_notes(vault, prompt, ctx["sources"], deps, model, emit)
            else:
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
        finally:
            vault.end_job()


MIN_NOTES_CHARS = 1500


async def _write_from_notes(vault: Vault, prompt: str, sources: list[str], deps: JobContext,
                            model: str | None, emit: Emit) -> tuple[JobReport, list]:
    """Read the sources in parts (claims recorded by code), then let the agent write the pages
    from the notes. If the provider refuses the request as too large (small per-request
    budgets), the notes are halved and the writing step runs again; the reading isn't repeated."""
    all_notes = [await read_in_parts(vault, s, emit, deps.claims, model) for s in sources]
    cap = get_settings().capture_notes_chars
    while True:
        try:
            return await _agent_turn(prompt + _notes_section(all_notes, deps.claims, cap), deps, model, emit)
        except Exception as e:
            if not is_too_large(e) or cap <= MIN_NOTES_CHARS:
                raise
            cap //= 2
            await emit({"type": "info", "message": "The request was too large for the model; writing "
                                                   f"again from shorter notes ({cap} characters)."})


def _notes_section(all_notes: list, claims: list[dict], cap: int) -> str:
    per = cap // len(all_notes)
    blocks = [f"## {n.source}\n{n.render(per)}" for n in all_notes]
    out = ("\n\n# Reading notes\nEvery quote is copied exactly from the source: rely on it and cite "
           "the source.\n\n" + "\n\n".join(blocks))
    if claims:
        lines = [f"- {c['enzyme']} {'de' if c.get('mechanism') == 'dephosphorylation' else ''}phosphorylates "
                 f"{c['target']}{' at ' + c['site'] if c.get('site') else ''} ({c.get('source')}): "
                 f"{c.get('status', 'unknown')}" for c in claims[:20]]
        out += "\n\n# Relations (recorded; status against the databases)\n" + "\n".join(lines)
    return out


async def _agent_turn(prompt: str, deps: JobContext, model: str | None, emit: Emit,
                      history: list | None = None) -> tuple[JobReport, list]:
    """One agent turn. If the provider rejects a tool the model made up, the turn is run once
    more with the exact tool names spelled out. If the provider is briefly unavailable (503,
    per-minute 429...), the turn is run again after a wait, up to len(TRANSIENT_WAITS) times.
    Work already done (pages written, claims recorded) stays, and the whole job still rolls
    back if the retries fail too."""
    bad_tool, waits = False, list(TRANSIENT_WAITS)
    while True:
        report, messages = None, []
        turn_prompt = prompt if not bad_tool else (
            prompt + "\n\nNote: a previous attempt called a tool that doesn't exist. Use only these "
            f"tools, spelled exactly: {', '.join(sorted(job_tool_names(deps.kind)))}.")
        try:
            async for ev in stream_run(job_agent(), turn_prompt, deps=deps, history=history,
                                       request_limit=get_settings().job_request_limit, model=model,
                                       role="job"):
                if ev["type"] == "result":
                    report, messages = ev["output"], ev["messages"]
                    await emit({"type": "usage", **ev["usage"]})
                elif ev["type"] != "graph":  # networks are for chat answers, not job logs
                    await emit(ev)
        except Exception as e:
            if not bad_tool and is_bad_tool_call(e):
                bad_tool = True
                await emit({"type": "info", "message": "The model called a tool that doesn't exist; retrying this step."})
                continue
            if waits and is_transient(e):
                wait = waits.pop(0)
                what = f"HTTP {e.status_code}" if hasattr(e, "status_code") else "no response in time"
                await emit({"type": "info", "message": f"The model provider is busy ({what}); "
                                                       f"retrying this step in {wait} s."})
                await asyncio.sleep(wait)
                continue
            raise
        assert report is not None
        return report, messages


# Waits before retrying a step the provider refused as busy (503, per-minute 429...).
TRANSIENT_WAITS = (20, 60, 120)


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
    deps.writes.clear()  # the revision may rewrite any page, whatever the writing turn did
    revised, _ = await _agent_turn(prompt, deps, model, emit, history=messages)
    return result, revised
