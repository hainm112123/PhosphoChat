"""In-process job manager: jobs run as asyncio tasks, serialised per vault by Vault.lock.

Job records and their event logs live in memory (prototype). Subscribers follow a job's
events over SSE from any offset, so a page reload can replay progress.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agents.jobs import JobKind, run_job
from app.agents.runtime import explain_error
from app.vault.store import StagedSource, Vault

MAX_JOBS = 500


@dataclass
class Job:
    id: str
    vault: str
    kind: JobKind
    params: dict
    status: str = "queued"  # queued | running | succeeded | failed
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    finished_at: str | None = None
    events: list[dict] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    # A capture's staged source (text and file bytes): in memory only, never sent to the UI.
    # Kept after a failure so the job can be retried; dropped once it succeeds.
    payload: StagedSource | None = field(default=None, repr=False)
    _changed: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed")

    async def emit(self, event: dict) -> None:
        async with self._changed:
            self.events.append(event)
            self._changed.notify_all()

    async def follow(self, start: int = 0):
        """Yield events from `start`, waiting for new ones until the job finishes."""
        i = start
        while True:
            async with self._changed:
                while i >= len(self.events) and not self.done:
                    await self._changed.wait()
                pending = self.events[i:]
            for ev in pending:
                yield ev
            i += len(pending)
            if self.done and i >= len(self.events):
                return

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "vault": self.vault, "kind": self.kind, "params": self.params,
                "status": self.status, "created_at": self.created_at,
                "finished_at": self.finished_at, "result": self.result, "error": self.error,
                "n_events": len(self.events), "retryable": self.retryable}

    @property
    def retryable(self) -> bool:
        return self.status == "failed" and (self.kind != "capture" or self.payload is not None)


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()

    def submit(self, vault: Vault, kind: JobKind, params: dict | None = None,
               payload: StagedSource | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], vault=vault.name, kind=kind, params=params or {},
                  payload=payload)
        self.jobs[job.id] = job
        while len(self.jobs) > MAX_JOBS:
            oldest = next(iter(self.jobs))
            if not self.jobs[oldest].done:
                break
            del self.jobs[oldest]
        task = asyncio.create_task(self._run(job, vault))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, vault: Vault) -> None:
        if vault.lock.locked():
            await job.emit({"type": "info", "message": "Waiting for another job on this vault..."})
        try:
            async def emit(ev: dict) -> None:
                if job.status == "queued":
                    job.status = "running"
                await job.emit(ev)
            job.result = await run_job(vault, job.kind, job.params, emit, staged=job.payload)
            job.payload = None  # saved in the vault now; free the memory
            final, status = {"type": "done", "result": job.result}, "succeeded"
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            job.error = explain_error(e)
            final, status = {"type": "error", "message": job.error}, "failed"
        # Append the final event and flip the status under the same lock, so followers
        # never see "done" without having been handed the final event.
        async with job._changed:
            job.events.append(final)
            job.status = status
            job.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            job._changed.notify_all()

    def retry(self, job: Job, vault: Vault) -> Job:
        """Run a failed job again with the same inputs (for a capture: the same staged source)."""
        if not job.retryable:
            raise ValueError("Only failed jobs can be retried, and a capture only while its "
                             "source is still in memory (not after a server restart).")
        params = {k: v for k, v in job.params.items() if k != "source"}  # re-saved by the new run
        new = self.submit(vault, job.kind, {**params, "retry_of": job.id}, payload=job.payload)
        job.payload = None  # the new job owns it now
        return new

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def for_vault(self, vault: str, limit: int = 30) -> list[Job]:
        return [j for j in reversed(self.jobs.values()) if j.vault == vault][:limit]


jobs = JobManager()
