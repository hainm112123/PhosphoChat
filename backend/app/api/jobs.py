"""Capture and the other write jobs, plus job status and progress (SSE)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sse_starlette import EventSourceResponse

from app.api.deps import get_vault
from app.jobs.runner import jobs
from app.tools.web import FetchError, fetch_url
from app.vault.store import DuplicateSource, VaultError, extract_text

router = APIRouter(prefix="/api")

MAX_UPLOAD = 20_000_000


class JobOptions(BaseModel):
    source: str | None = None  # capture: re-run on an already saved source
    days: int = 7              # digest period
    end: str | None = None     # digest end date (YYYY-MM-DD)
    model: str | None = None


class SaveAnswer(BaseModel):
    question: str
    answer: str
    title: str | None = None


def _save_or_raise(fn):
    try:
        return fn()
    except DuplicateSource as e:
        raise HTTPException(409, str(e)) from e
    except VaultError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/vaults/{vault}/capture", status_code=202)
async def capture(vault: str, url: str | None = Form(None), text: str | None = Form(None),
                  title: str | None = Form(None), file: UploadFile | None = File(None)) -> dict:
    """Save one source (URL, pasted text or file), then start a capture job on it."""
    v = get_vault(vault)
    given = [x for x in (url and url.strip(), text and text.strip(), file) if x]
    if len(given) != 1:
        raise HTTPException(400, "Provide exactly one of: url, text, file.")

    if url and url.strip():
        try:
            fetched = await fetch_url(url)
        except FetchError as e:
            raise HTTPException(400, str(e)) from e
        name = _save_or_raise(lambda: v.save_source(
            title=title or fetched.title, body=fetched.markdown, source_url=fetched.url,
            content_type=fetched.content_type))
    elif text and text.strip():
        first = text.strip().splitlines()[0].lstrip("# ").strip()
        name = _save_or_raise(lambda: v.save_source(
            title=title or first[:80] or "Pasted note", body=text.strip(),
            source_url="pasted", content_type="note"))
    else:
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "File is larger than 20 MB.")
        filename = Path(file.filename or "upload.txt").name
        body, ctype = _save_or_raise(lambda: extract_text(filename, data))
        name = _save_or_raise(lambda: v.save_source(
            title=title or Path(filename).stem, body=body, source_url=f"upload:{filename}",
            content_type=ctype, original=(filename, data)))

    job = jobs.submit(v, "capture", {"source": name})
    return {"job": job.summary(), "source": name}


@router.post("/vaults/{vault}/chat/save", status_code=202)
def save_answer(vault: str, body: SaveAnswer) -> dict:
    """Save a chat answer as a note source and capture it into the wiki."""
    v = get_vault(vault)
    title = body.title or body.question.strip().splitlines()[0][:80] or "Chat answer"
    note = f"## Question\n\n{body.question.strip()}\n\n## Answer\n\n{body.answer.strip()}\n"
    name = _save_or_raise(lambda: v.save_source(
        title=title, body=note, source_url="phosphochat:chat", content_type="note"))
    job = jobs.submit(v, "capture", {"source": name})
    return {"job": job.summary(), "source": name}


@router.post("/vaults/{vault}/jobs/{kind}", status_code=202)
def start_job(vault: str, kind: Literal["capture", "sync", "lint", "digest"],
              options: JobOptions | None = None) -> dict:
    v = get_vault(vault)
    opts = options or JobOptions()
    if kind == "capture":
        names = {s["name"] for s in v.list_sources()}
        if not opts.source or opts.source not in names or not opts.source.endswith(".md"):
            raise HTTPException(400, "capture needs 'source': an existing source note (.md).")
    return jobs.submit(v, kind, opts.model_dump(exclude_none=True)).summary()


@router.get("/vaults/{vault}/jobs")
def vault_jobs(vault: str) -> list[dict]:
    get_vault(vault)
    return [j.summary() for j in jobs.for_vault(vault)]


@router.get("/jobs/{job_id}")
def job(job_id: str) -> dict:
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "No such job.")
    return {**j.summary(), "events": j.events}


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, start: int = 0):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "No such job.")

    async def gen():
        async for ev in j.follow(start):
            yield {"event": ev["type"], "data": json.dumps(ev, default=str)}

    return EventSourceResponse(gen())
