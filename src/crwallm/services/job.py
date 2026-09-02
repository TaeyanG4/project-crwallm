"""Job lifecycle.

The API and the CLI both come through here, so the two surfaces cannot drift
(docs/03_SYSTEM_ARCHITECTURE.md). Phase 2 keeps this to the shell: submit,
claim, look at. Cancellation, retries, stale recovery and resume arrive in
Phase 8 and hang off the same rows.

**Why the queue is PostgreSQL.** ``FOR UPDATE SKIP LOCKED`` gives an atomic
claim with no second piece of infrastructure to run, back up or keep in sync
with the data it points at. Redis earns its place when this is measured to be
the bottleneck, and not before (docs/17_NON_GOALS.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from crwallm.db.models import CrawlJob, CrawlSpecRow, JobStatus
from crwallm.schemas.spec import CrawlSpec
from crwallm.schemas.types import ErrorKind

__all__ = ["JobService"]


STALE_AFTER_S = 120.0
"""How long without a heartbeat before a job is assumed abandoned.

Comfortably longer than the sink's flush interval, so a worker that is merely
busy on one very slow page is not reaped out from under itself. Shorter than
anyone's patience for a stuck job."""

MAX_ATTEMPTS = 3
"""How many times a job may be requeued before it is called failed.

A job that three workers have died on is not unlucky; something about it kills
workers, and requeueing it forever would take the queue down with it."""


class JobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def submit(self, spec: CrawlSpec, *, priority: int = 0) -> CrawlJob:
        """Store the spec, queue a job, return immediately.

        The response has to come back before the crawl runs: a five-hundred
        page crawl takes minutes and cannot live inside an HTTP request.
        """
        row = CrawlSpecRow(
            id=spec.id,
            name=spec.name,
            payload=spec.model_dump(mode="json"),
        )
        self._session.add(row)

        job = CrawlJob(spec_id=spec.id, status=JobStatus.QUEUED, priority=priority)
        self._session.add(job)
        await self._session.commit()
        await self._session.refresh(job)
        return job

    async def claim(self, worker_id: str) -> tuple[CrawlJob, CrawlSpec] | None:
        """Take the next queued job, atomically.

        ``SKIP LOCKED`` is what makes several workers safe against one queue:
        each transaction locks a different row rather than queueing behind the
        same one.
        """
        stmt = (
            select(CrawlJob)
            .where(CrawlJob.status == JobStatus.QUEUED)
            .order_by(CrawlJob.priority.desc(), CrawlJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await self._session.execute(stmt)).scalar_one_or_none()
        if job is None:
            await self._session.rollback()
            return None

        now = datetime.now(UTC)
        job.status = JobStatus.RUNNING
        job.worker_id = worker_id
        job.started_at = now
        job.heartbeat_at = now
        await self._session.commit()

        spec_row = await self._session.get(CrawlSpecRow, job.spec_id)
        if spec_row is None:  # pragma: no cover - FK makes this impossible
            raise RuntimeError(f"job {job.id} references a missing spec")

        return job, CrawlSpec.model_validate(spec_row.payload)

    async def get(self, job_id: UUID) -> CrawlJob | None:
        return await self._session.get(CrawlJob, job_id)

    async def list_recent(self, *, limit: int = 20, status: str | None = None) -> list[CrawlJob]:
        stmt = select(CrawlJob).order_by(CrawlJob.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(CrawlJob.status == status)
        return list((await self._session.execute(stmt)).scalars())

    async def mark_failed(self, job_id: UUID, kind: str, message: str) -> None:
        """Record a failure the crawl could not report itself.

        The sink writes terminal state for anything the engine emitted; this
        covers the case where the worker died before the engine could - an
        unhandled exception, or a spec the gate refused outright.
        """
        await self._session.execute(
            update(CrawlJob)
            .where(CrawlJob.id == job_id)
            .values(
                status=JobStatus.FAILED,
                error_kind=kind,
                error_message=message[:2000],
                completed_at=datetime.now(UTC),
            )
        )
        await self._session.commit()

    async def heartbeat(self, job_id: UUID) -> None:
        """Say the worker is alive.

        Nothing reads this until Phase 8's stale recovery, but a job that dies
        mid-crawl without one leaves a row stuck in ``running`` with no way to
        tell it apart from a slow one.
        """
        await self._session.execute(
            update(CrawlJob).where(CrawlJob.id == job_id).values(heartbeat_at=datetime.now(UTC))
        )
        await self._session.commit()

    # ------------------------------------------------------------ durability

    async def request_cancel(self, job_id: UUID) -> bool:
        """Ask a job to stop. Returns False if it already had.

        A request, not an act. A queued job can be cancelled outright, but a
        running one is inside somebody else's event loop and the only honest
        thing to do is leave a note the worker will read between pages. The
        alternative - killing the task - loses the records already extracted
        and the archive already written (docs/09_JOB_ARCHITECTURE.md).
        """
        job = await self._session.get(CrawlJob, job_id)
        if job is None or job.status in JobStatus.TERMINAL:
            return False

        now = datetime.now(UTC)
        job.cancel_requested_at = now
        if job.status == JobStatus.QUEUED:
            # Nothing is running it, so there is nobody to notice the note.
            job.status = JobStatus.CANCELLED
            job.completed_at = now
        await self._session.commit()
        return True

    async def cancel_requested(self, job_id: UUID) -> bool:
        """Whether a stop has been asked for.

        Read between pages by the worker. Its own session, and a fresh read
        every time: the request is written by the API in a different process,
        so a cached row would never show it.
        """
        result = await self._session.execute(
            select(CrawlJob.cancel_requested_at).where(CrawlJob.id == job_id)
        )
        return result.scalar_one_or_none() is not None

    async def mark_cancelled(self, job_id: UUID) -> None:
        await self._session.execute(
            update(CrawlJob)
            .where(CrawlJob.id == job_id, CrawlJob.status == JobStatus.RUNNING)
            .values(status=JobStatus.CANCELLED, completed_at=datetime.now(UTC))
        )
        await self._session.commit()

    async def reap_stale(self, *, older_than_s: float = STALE_AFTER_S) -> list[UUID]:
        """Return jobs whose worker stopped reporting to the queue.

        A worker that is killed - a crash, a laptop lid, a container
        rescheduled - leaves its job in ``running`` forever, indistinguishable
        from one that is merely slow. The heartbeat is what tells them apart,
        and this is the only thing that reads it.

        Requeued rather than failed. The records already written stay, the
        unique constraint on ``(job_id, page_url, record_hash)`` makes writing
        them again a no-op, and a job that died to a power cut deserves
        another attempt more than it deserves a failure row.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_s)
        stale = (
            await self._session.execute(
                select(CrawlJob)
                .where(
                    CrawlJob.status == JobStatus.RUNNING,
                    # A job claimed but never heartbeaten is stale too: the
                    # worker died between claiming and its first flush.
                    func.coalesce(CrawlJob.heartbeat_at, CrawlJob.started_at) < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()

        reaped: list[UUID] = []
        for job in stale:
            job.status = JobStatus.QUEUED
            job.worker_id = None
            job.started_at = None
            job.heartbeat_at = None
            job.attempts += 1
            if job.attempts > MAX_ATTEMPTS:
                job.status = JobStatus.FAILED
                job.error_kind = ErrorKind.INTERNAL.value
                job.error_message = (
                    f"abandoned by a worker {job.attempts} times; not requeued again"
                )
                job.completed_at = datetime.now(UTC)
            reaped.append(job.id)

        if reaped:
            await self._session.commit()
        return reaped

    async def retry(self, job_id: UUID) -> bool:
        """Run a finished job again, from the beginning.

        Distinct from resume, which continues from where a crawl stopped.
        Retry is the honest answer when the *reason* it failed has been fixed
        - a recipe corrected, a site back up - and starting over is what makes
        the result trustworthy (docs/09_JOB_ARCHITECTURE.md).

        The counters reset; the records do not. Re-collecting a page writes
        the same rows, and the unique constraint absorbs them.
        """
        job = await self._session.get(CrawlJob, job_id)
        if job is None or job.status not in JobStatus.TERMINAL:
            return False

        job.status = JobStatus.QUEUED
        job.worker_id = None
        job.started_at = None
        job.completed_at = None
        job.heartbeat_at = None
        job.cancel_requested_at = None
        job.error_kind = None
        job.error_message = None
        job.pages_crawled = 0
        job.pages_failed = 0
        job.records_extracted = 0
        job.error_counts = {}
        job.reject_counts = {}
        job.attempts += 1
        await self._session.commit()
        return True
