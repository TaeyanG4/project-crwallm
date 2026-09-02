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

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crwallm.api.deps import session_dep, sessionmaker_dep, token_dep
from crwallm.api.schemas import (
    JobDetail,
    JobEvent,
    JobSubmitRequest,
    JobSubmitted,
    JobSummary,
    PageRow,
    PageRowList,
    RecordPage,
    RecordSource,
)
from crwallm.db.models import CrawlEventRow, CrawlResult, ExtractedRecord, JobStatus
from crwallm.policy.domains import InvalidDomainError
from crwallm.services.export import (
    EXPORT_FORMATS,
    content_type_for,
    export_records,
    filename_for,
)
from crwallm.services.job import JobService

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

Session = Annotated[AsyncSession, Depends(session_dep)]
SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(sessionmaker_dep)]


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
    materialised = list(rows)

    return RecordPage(
        job_id=job_id,
        total_returned=len(materialised),
        offset=offset,
        limit=limit,
        records=[r.data for r in materialised],
        provenance=[RecordSource.model_validate(r) for r in materialised],
    )


@router.get("/{job_id}/pages", response_model=PageRowList)
async def get_pages(
    job_id: UUID,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageRowList:
    """What the crawl fetched, as opposed to what it extracted.

    A crawl that reports "200 pages, 0 records" is answered here and nowhere
    else: the pages are all 200s and the recipe missed, or they are all 404s
    and the seeds were wrong. The records endpoint cannot tell those apart.
    """
    if await JobService(session).get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")

    rows = (
        await session.execute(
            select(CrawlResult)
            .where(CrawlResult.job_id == job_id)
            .order_by(CrawlResult.created_at, CrawlResult.id)
            .offset(offset)
            .limit(limit)
        )
    ).scalars()

    pages = [PageRow.model_validate(r) for r in rows]
    return PageRowList(
        job_id=job_id, total_returned=len(pages), offset=offset, limit=limit, pages=pages
    )


@router.get("/{job_id}/events", response_model=list[JobEvent])
async def get_events(
    job_id: UUID,
    session: Session,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[JobEvent]:
    """The event log as a plain page, for a caller that does not want a stream."""
    if await JobService(session).get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")

    rows = (
        await session.execute(
            select(CrawlEventRow)
            .where(CrawlEventRow.job_id == job_id, CrawlEventRow.id > after)
            .order_by(CrawlEventRow.id)
            .limit(limit)
        )
    ).scalars()
    return [JobEvent.model_validate(r) for r in rows]


SSE_POLL_S = 0.5
SSE_IDLE_TIMEOUT_S = 900.0


async def _event_stream(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: UUID, cursor: int
) -> AsyncIterator[str]:
    """Tail one job's event log.

    Polling rather than LISTEN/NOTIFY: the writer is a separate process
    batching its inserts, so a notification would only ever say "look again",
    which is what a half-second poll already does. This is a local
    single-user tool - one cheap indexed query per client per tick is not a
    load worth engineering around (docs/09_JOB_ARCHITECTURE.md).

    Each message carries its row id as the SSE ``id:`` field, so a browser
    that drops the connection reconnects with ``Last-Event-ID`` and resumes
    rather than replaying the crawl from the beginning.

    Its own session, not the request's: this outlives the handler, and
    holding the request's session open for the life of the stream would
    keep a connection checked out of the pool for as long as the crawl runs.
    """
    waited = 0.0

    while True:
        async with sessionmaker() as session:
            # Status first, events second, and the order is load-bearing.
            # The worker writes a batch of events and the terminal status in
            # one transaction. Reading events first would let that commit land
            # between the two queries: no new rows, job already finished, and
            # the stream would end having dropped the events it just missed -
            # including JobCompleted. Read the other way round and a status
            # that says "finished" guarantees the events are already visible.
            job = await JobService(session).get(job_id)
            finished = job is None or job.status in JobStatus.TERMINAL
            rows = list(
                (
                    await session.execute(
                        select(CrawlEventRow)
                        .where(CrawlEventRow.job_id == job_id, CrawlEventRow.id > cursor)
                        .order_by(CrawlEventRow.id)
                        .limit(500)
                    )
                ).scalars()
            )

        for row in rows:
            cursor = row.id
            body = json.dumps(
                {
                    "id": row.id,
                    "event_type": row.event_type,
                    "payload": row.payload,
                    "created_at": row.created_at.isoformat(),
                },
                ensure_ascii=False,
            )
            yield f"id: {row.id}\nevent: {row.event_type}\ndata: {body}\n\n"

        if rows:
            waited = 0.0
        else:
            # Nothing new and the job was already finished when we looked:
            # the log is drained.
            if finished:
                yield "event: end\ndata: {}\n\n"
                return
            if waited >= SSE_IDLE_TIMEOUT_S:
                yield "event: timeout\ndata: {}\n\n"
                return
            # A comment frame, not an event: it keeps proxies and the browser
            # from closing an idle connection without being mistaken for data.
            yield ": keepalive\n\n"
            await asyncio.sleep(SSE_POLL_S)
            waited += SSE_POLL_S


@router.get("/{job_id}/stream")
async def stream_events(
    job_id: UUID,
    session: Session,
    sessionmaker: SessionFactory,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Server-sent events for one job, live.

    Polling ``GET /api/jobs/{id}`` shows counters moving but never says what
    the crawl is doing. This is the difference between "37 pages" and "37
    pages, currently on /shop/page/4, three rejected as out of scope".
    """
    if await JobService(session).get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")

    cursor = after
    if last_event_id is not None and last_event_id.isdigit():
        # The header wins: it is the browser telling us where it actually got
        # to, which is better information than a query string built by hand.
        cursor = int(last_event_id)

    return StreamingResponse(
        _event_stream(sessionmaker, job_id, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx and friends buffer text/event-stream by default, which
            # turns a live feed into one delivery at the end.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{job_id}/cancel", response_model=JobDetail, dependencies=[Depends(token_dep)])
async def cancel(job_id: UUID, session: Session) -> JobDetail:
    """Ask a running crawl to stop.

    202-ish semantics with a 200 body: the note is written now and the worker
    reads it between pages, so a job that was running comes back still
    ``running`` and settles a moment later. Returning the row rather than an
    empty body lets the caller see which of the two happened.
    """
    service = JobService(session)
    if await service.get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")

    if not await service.request_cancel(job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="job has already finished",
        )

    job = await service.get(job_id)
    assert job is not None
    return JobDetail.model_validate(job)


@router.post("/{job_id}/retry", response_model=JobDetail, dependencies=[Depends(token_dep)])
async def retry(job_id: UUID, session: Session) -> JobDetail:
    """Run a finished job again from the beginning.

    Counters reset; records do not. Re-collecting a page writes the same rows
    and the unique constraint absorbs them, which is what makes this safe to
    press twice (docs/09_JOB_ARCHITECTURE.md).
    """
    service = JobService(session)
    if await service.get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")

    if not await service.retry(job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only a finished job can be retried",
        )

    job = await service.get(job_id)
    assert job is not None
    return JobDetail.model_validate(job)


@router.get("/{job_id}/export")
async def export(
    job_id: UUID,
    session: Session,
    fmt: Annotated[str, Query(alias="format")] = "jsonl",
    include_source: Annotated[bool, Query()] = False,
) -> StreamingResponse:
    """Download a job's records.

    Streamed, and a download rather than a page: half a million records must
    not be assembled in memory on either side, and a browser asked to render
    them as a document would try.
    """
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown format {fmt!r}; expected one of {list(EXPORT_FORMATS)}",
        )
    if await JobService(session).get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")

    return StreamingResponse(
        export_records(session, job_id, fmt, include_source=include_source),
        media_type=content_type_for(fmt),
        headers={"Content-Disposition": f'attachment; filename="{filename_for(job_id, fmt)}"'},
    )
