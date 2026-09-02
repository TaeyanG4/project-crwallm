"""Job endpoints.

Thin by design: every route validates its input, calls a service function and
shapes the answer. The same ``JobService`` backs the CLI, so the two surfaces
cannot drift - and if a route ever needs to reach past ``services`` to get its
work done, the boundary was drawn in the wrong place
(docs/03_SYSTEM_ARCHITECTURE.md).

Submission is guarded by the token dependency. That matters more here than it
looks: a page the user happens to be visiting can POST to ``127.0.0.1``, and
without the guard it would be submitting crawls (docs/11_SECURITY_MODEL.md
section 1). Reads are left open - they expose nothing a local user cannot
already see, and requiring the header on them would make the browsable API
useless for no gain.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from crwallm.api.deps import session_dep, token_dep
from crwallm.api.schemas import (
    JobDetail,
    JobSubmitRequest,
    JobSubmitted,
    JobSummary,
    RecordPage,
)
from crwallm.db.models import ExtractedRecord, JobStatus
from crwallm.policy.domains import InvalidDomainError
from crwallm.services.job import JobService

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

Session = Annotated[AsyncSession, Depends(session_dep)]


@router.post(
    "",
    response_model=JobSubmitted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(token_dep)],
)
async def submit(request: JobSubmitRequest, session: Session) -> JobSubmitted:
    """Queue a crawl.

    202, not 201: the job is accepted, not finished, and the caller polls for
    the rest.

    The domain scope is validated here rather than left to the worker. A spec
    that can never run should fail at the point somebody can still fix it, not
    an hour later in a log.
    """
    try:
        from crwallm.policy.domains import validate_allowed_domains

        validate_allowed_domains(request.spec.allowed_domains)
    except InvalidDomainError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    job = await JobService(session).submit(request.spec, priority=request.priority)
    return JobSubmitted(id=job.id, status=job.status, created_at=job.created_at)


@router.get("", response_model=list[JobSummary])
async def list_jobs(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    job_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[JobSummary]:
    if job_status is not None and job_status not in JobStatus.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown status {job_status!r}; expected one of {list(JobStatus.ALL)}",
        )
    jobs = await JobService(session).list_recent(limit=limit, status=job_status)
    return [JobSummary.model_validate(j) for j in jobs]


@router.get("/{job_id}", response_model=JobDetail)
async def get_job(job_id: UUID, session: Session) -> JobDetail:
    job = await JobService(session).get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")
    return JobDetail.model_validate(job)


@router.get("/{job_id}/results", response_model=RecordPage)
async def get_results(
    job_id: UUID,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecordPage:
    """Extracted records, oldest first.

    404s for a job that does not exist rather than returning an empty page -
    "no records yet" and "wrong id" are different answers and a caller polling
    for results needs to tell them apart.
    """
    if await JobService(session).get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")

    rows = (
        await session.execute(
            select(ExtractedRecord)
            .where(ExtractedRecord.job_id == job_id)
            .order_by(ExtractedRecord.created_at, ExtractedRecord.id)
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    records = [r.data for r in rows]

    return RecordPage(
        job_id=job_id,
        total_returned=len(records),
        offset=offset,
        limit=limit,
        records=records,
    )
