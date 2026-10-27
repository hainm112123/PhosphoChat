"""Capture and the other write jobs, plus job status and progress (SSE)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sse_starlette import EventSourceResponse

from app.api.deps import get_vault
from app.auth.deps import require_admin
from app.jobs.runner import jobs
from app.tools.web import FetchError, fetch_url
from app.vault.store import DuplicateSource, StagedSource, VaultError, extract_text

# Everything here changes a vault or reports on management jobs: admins only.
router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])

MAX_UPLOAD = 20_000_000


class JobOptions(BaseModel):
    days: int = 7              # digest period
    end: str | None = None     # digest end date (YYYY-MM-DD)
    model: str | None = None


class SaveAnswer(BaseModel):
    question: str
    answer: str
    title: str | None = None


def _or_http(fn):
    try:
        return fn()
    except DuplicateSource as e:
        raise HTTPException(409, str(e)) from e
    except VaultError as e:
        raise HTTPException(400, str(e)) from e


def _submit_capture(v, staged: StagedSource, verify: bool | None = None) -> dict:
    """Reject duplicates now (fast feedback), then hand the staged source to a capture job.
    Nothing is written to the vault here: the job saves the source inside its transaction.
    `verify` turns the fact-check on/off for this capture (None = VERIFY_CAPTURES)."""
    _or_http(lambda: v.check_new_source(staged.source_url, staged.digest))
    params = {"title": staged.title, "source_url": staged.source_url}
    if verify is not None:
        params["verify"] = verify
    job = jobs.submit(v, "capture", params, payload=staged)
    return {"job": job.summary()}


@router.post("/vaults/{vault}/capture", status_code=202)
async def capture(vault: str, url: str | None = Form(None), text: str | None = Form(None),
                  title: str | None = Form(None), file: UploadFile | None = File(None),
                  verify: bool | None = Form(None)) -> dict:
    """Capture one source (URL, pasted text or file). The source is saved only if the whole
    capture succeeds."""
    v = get_vault(vault)
    given = [x for x in (url and url.strip(), text and text.strip(), file) if x]
    if len(given) != 1:
        raise HTTPException(400, "Provide exactly one of: url, text, file.")

    if url and url.strip():
        try:
            fetched = await fetch_url(url)
        except FetchError as e:
            raise HTTPException(400, str(e)) from e
        staged = StagedSource(title=title or fetched.title, body=fetched.markdown,
                              source_url=fetched.url, content_type=fetched.content_type)
    elif text and text.strip():
        first = text.strip().splitlines()[0].lstrip("# ").strip()
        staged = StagedSource(title=title or first[:80] or "Pasted note", body=text.strip(),
                              source_url="pasted", content_type="note")
    else:
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "File is larger than 20 MB.")
        filename = Path(file.filename or "upload.txt").name
        body, ctype = _or_http(lambda: extract_text(filename, data))
        staged = StagedSource(title=title or Path(filename).stem, body=body,
                              source_url=f"upload:{filename}", content_type=ctype,
                              original=(filename, data))
    return _submit_capture(v, staged, verify)


@router.post("/vaults/{vault}/chat/save", status_code=202)
async def save_answer(vault: str, body: SaveAnswer) -> dict:
    """Capture a chat answer into the wiki as a note source."""
    v = get_vault(vault)
    title = body.title or body.question.strip().splitlines()[0][:80] or "Chat answer"
    note = f"## Question\n\n{body.question.strip()}\n\n## Answer\n\n{body.answer.strip()}\n"
    return _submit_capture(v, StagedSource(title=title, body=note, source_url="phosphochat:chat",
                                           content_type="note"))


@router.post("/vaults/{vault}/jobs/{kind}", status_code=202)
async def start_job(vault: str, kind: Literal["sync", "lint", "digest"],
                    options: JobOptions | None = None) -> dict:
    v = get_vault(vault)
    opts = options or JobOptions()
    return jobs.submit(v, kind, opts.model_dump(exclude_none=True)).summary()


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str) -> dict:
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "No such job.")
    if j.kind == "capture" and j.status == "failed" and j.payload is None:
        raise HTTPException(410, "This capture's source is no longer in memory (the server "
                                 "restarted or it was already retried). Capture it again.")
    try:
        return jobs.retry(j, get_vault(j.vault)).summary()
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


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
