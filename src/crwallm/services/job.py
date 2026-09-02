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

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from crwallm.db.models import CrawlJob, CrawlSpecRow, JobStatus
from crwallm.schemas.spec import CrawlSpec

__all__ = ["JobService"]


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
