"""Cancel, stale recovery and retry, against a real queue.

Every one of these is about what happens when something goes wrong at an
awkward moment - a worker killed mid-crawl, a stop requested between pages, a
job retried after a fix. A stub session cannot exhibit any of it, because the
behaviour under test *is* the transaction.

docs/09_JOB_ARCHITECTURE.md
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from crwallm.db.models import CrawlJob, JobStatus
from crwallm.schemas.spec import CrawlLimits, CrawlSpec
from crwallm.services.job import MAX_ATTEMPTS, JobService
from tests.integration.test_job_pipeline import TEST_DATABASE_URL, _database_reachable

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine():  # type: ignore[no-untyped-def]
    if not await _database_reachable():
        pytest.skip(f"no database at {TEST_DATABASE_URL.split('@')[-1]}")

    from crwallm.db.base import Base

    eng = create_async_engine(TEST_DATABASE_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def clean(engine) -> AsyncIterator[None]:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE crawl_specs, crawl_jobs, crawl_results, "
                "extracted_records, crawl_events RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture(loop_scope="module")
async def maker(engine):  # type: ignore[no-untyped-def]
    return async_sessionmaker(engine, expire_on_commit=False)


def spec() -> CrawlSpec:
    return CrawlSpec(
        seed_urls=("https://shop.test/list",),
        allowed_domains=("shop.test",),
        limits=CrawlLimits(max_pages=5, max_depth=1),
    )


async def submit(maker) -> uuid.UUID:  # type: ignore[no-untyped-def]
    async with maker() as session:
        job = await JobService(session).submit(spec())
        return job.id


async def status_of(maker, job_id: uuid.UUID) -> str:  # type: ignore[no-untyped-def]
    async with maker() as session:
        job = await JobService(session).get(job_id)
        assert job is not None
        return job.status


async def age_the_heartbeat(maker, job_id: uuid.UUID, seconds: float) -> None:  # type: ignore[no-untyped-def]
    """Backdate a job's heartbeat, as a dead worker's would be."""
    async with maker() as session:
        job = await session.get(CrawlJob, job_id)
        assert job is not None
        job.heartbeat_at = datetime.now(UTC) - timedelta(seconds=seconds)
        await session.commit()


class TestCancel:
    async def test_a_queued_job_is_cancelled_outright(self, maker) -> None:  # type: ignore[no-untyped-def]
        """Nothing is running it, so there is nobody to read a note."""
        job_id = await submit(maker)
        async with maker() as session:
            assert await JobService(session).request_cancel(job_id)
        assert await status_of(maker, job_id) == JobStatus.CANCELLED

    async def test_a_running_job_only_gets_a_note(self, maker) -> None:  # type: ignore[no-untyped-def]
        """Killing the task would lose the records already extracted and the
        archive already written. The worker stops between pages instead."""
        job_id = await submit(maker)
        async with maker() as session:
            await JobService(session).claim("worker-1")

        async with maker() as session:
            assert await JobService(session).request_cancel(job_id)

        assert await status_of(maker, job_id) == JobStatus.RUNNING
        async with maker() as session:
            assert await JobService(session).cancel_requested(job_id)

    async def test_the_worker_sees_the_note_from_another_session(self, maker) -> None:  # type: ignore[no-untyped-def]
        """The request is written by the API in another process. A worker
        reading its own mid-transaction session would never see it."""
        job_id = await submit(maker)
        async with maker() as worker_session:
            await JobService(worker_session).claim("worker-1")

            async with maker() as api_session:
                await JobService(api_session).request_cancel(job_id)

            async with maker() as watcher:
                assert await JobService(watcher).cancel_requested(job_id)

    async def test_cancelling_a_finished_job_is_refused(self, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await submit(maker)
        async with maker() as session:
            service = JobService(session)
            await service.claim("worker-1")
            await service.mark_cancelled(job_id)

        async with maker() as session:
            assert not await JobService(session).request_cancel(job_id)

    async def test_a_job_with_no_note_is_not_cancelled(self, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await submit(maker)
        async with maker() as session:
            assert not await JobService(session).cancel_requested(job_id)


class TestStaleRecovery:
    async def test_a_job_whose_worker_died_is_requeued(self, maker) -> None:  # type: ignore[no-untyped-def]
        """The case this exists for: a crash, a closed laptop, a container
        rescheduled. Without it the row sits in `running` forever, looking
        exactly like a slow crawl."""
        job_id = await submit(maker)
        async with maker() as session:
            await JobService(session).claim("worker-that-dies")
        await age_the_heartbeat(maker, job_id, 600)

        async with maker() as session:
            reaped = await JobService(session).reap_stale()

        assert reaped == [job_id]
        assert await status_of(maker, job_id) == JobStatus.QUEUED

    async def test_a_live_job_is_left_alone(self, maker) -> None:  # type: ignore[no-untyped-def]
        """A worker busy on one very slow page must not be reaped out from
        under itself."""
        job_id = await submit(maker)
        async with maker() as session:
            await JobService(session).claim("worker-1")

        async with maker() as session:
            assert await JobService(session).reap_stale() == []
        assert await status_of(maker, job_id) == JobStatus.RUNNING

    async def test_a_job_claimed_but_never_heartbeaten_is_reaped(self, maker) -> None:  # type: ignore[no-untyped-def]
        """The worker died between claiming and its first flush, so there is
        no heartbeat at all - only a `started_at`."""
        job_id = await submit(maker)
        async with maker() as session:
            await JobService(session).claim("worker-1")
        async with maker() as session:
            job = await session.get(CrawlJob, job_id)
            assert job is not None
            job.heartbeat_at = None
            job.started_at = datetime.now(UTC) - timedelta(seconds=600)
            await session.commit()

        async with maker() as session:
            assert await JobService(session).reap_stale() == [job_id]

    async def test_a_requeued_job_can_be_claimed_again(self, maker) -> None:  # type: ignore[no-untyped-def]
        """Requeueing that produced an unclaimable row would be worse than
        leaving it stuck."""
        job_id = await submit(maker)
        async with maker() as session:
            await JobService(session).claim("worker-1")
        await age_the_heartbeat(maker, job_id, 600)
        async with maker() as session:
            await JobService(session).reap_stale()

        async with maker() as session:
            claimed = await JobService(session).claim("worker-2")
        assert claimed is not None
        assert claimed[0].id == job_id

    async def test_a_job_that_keeps_killing_workers_is_failed(self, maker) -> None:  # type: ignore[no-untyped-def]
        """Three workers have died on it. Requeueing forever would take the
        queue down with it."""
        job_id = await submit(maker)
        for _ in range(MAX_ATTEMPTS + 1):
            async with maker() as session:
                await JobService(session).claim("doomed")
            await age_the_heartbeat(maker, job_id, 600)
            async with maker() as session:
                await JobService(session).reap_stale()

        assert await status_of(maker, job_id) == JobStatus.FAILED
        async with maker() as session:
            job = await JobService(session).get(job_id)
            assert job is not None
            assert "abandoned" in (job.error_message or "")

    async def test_completed_jobs_are_never_reaped(self, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await submit(maker)
        async with maker() as session:
            job = await session.get(CrawlJob, job_id)
            assert job is not None
            job.status = JobStatus.COMPLETED
            job.heartbeat_at = datetime.now(UTC) - timedelta(days=1)
            await session.commit()

        async with maker() as session:
            assert await JobService(session).reap_stale() == []


class TestRetry:
    async def test_a_failed_job_runs_again_from_the_start(self, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await submit(maker)
        async with maker() as session:
            service = JobService(session)
            await service.claim("worker-1")
            await service.mark_failed(job_id, "http_5xx", "site was down")

        async with maker() as session:
            assert await JobService(session).retry(job_id)

        assert await status_of(maker, job_id) == JobStatus.QUEUED

    async def test_the_counters_reset_and_the_error_clears(self, maker) -> None:  # type: ignore[no-untyped-def]
        """A retried job showing the previous run's failure would be reporting
        a fact about a run that no longer exists."""
        job_id = await submit(maker)
        async with maker() as session:
            job = await session.get(CrawlJob, job_id)
            assert job is not None
            job.status = JobStatus.FAILED
            job.pages_crawled = 40
            job.records_extracted = 12
            job.error_kind = "http_5xx"
            job.error_counts = {"http_5xx": 40}
            await session.commit()

        async with maker() as session:
            await JobService(session).retry(job_id)

        async with maker() as session:
            job = await JobService(session).get(job_id)
            assert job is not None
            assert job.pages_crawled == 0
            assert job.records_extracted == 0
            assert job.error_kind is None
            assert job.error_counts == {}

    async def test_a_running_job_cannot_be_retried(self, maker) -> None:  # type: ignore[no-untyped-def]
        """It would be claimed twice and write two crawls into one row."""
        job_id = await submit(maker)
        async with maker() as session:
            await JobService(session).claim("worker-1")

        async with maker() as session:
            assert not await JobService(session).retry(job_id)

    async def test_a_cancelled_job_can_be_retried(self, maker) -> None:  # type: ignore[no-untyped-def]
        """Cancelling and then changing your mind is ordinary."""
        job_id = await submit(maker)
        async with maker() as session:
            await JobService(session).request_cancel(job_id)
        async with maker() as session:
            assert await JobService(session).retry(job_id)

    async def test_retrying_clears_a_previous_cancel_request(self, maker) -> None:  # type: ignore[no-untyped-def]
        """Otherwise the worker reads the old note and stops immediately."""
        job_id = await submit(maker)
        async with maker() as session:
            await JobService(session).request_cancel(job_id)
        async with maker() as session:
            await JobService(session).retry(job_id)

        async with maker() as session:
            assert not await JobService(session).cancel_requested(job_id)


class TestExport:
    """Getting the data out.

    The interesting parts are the ones a small fixture would not show: that
    the CSV header covers a key that only appears on the last row, that a list
    value survives, and that the whole thing streams rather than materialising.
    """

    async def seed_records(self, maker, rows: list[dict[str, object]]) -> uuid.UUID:  # type: ignore[no-untyped-def]
        import hashlib

        from crwallm.db.models import ExtractedRecord

        job_id = await submit(maker)
        async with maker() as session:
            for index, data in enumerate(rows):
                session.add(
                    ExtractedRecord(
                        id=uuid.uuid4(),
                        job_id=job_id,
                        page_url=f"https://shop.test/p/{index}",
                        extractor="css",
                        data=data,
                        record_hash=hashlib.blake2b(str(data).encode(), digest_size=16).hexdigest(),
                    )
                )
            await session.commit()
        return job_id

    async def collect(self, maker, job_id: uuid.UUID, fmt: str, **kw: object) -> str:  # type: ignore[no-untyped-def]
        from crwallm.services.export import export_records

        async with maker() as session:
            return "".join(
                [chunk async for chunk in export_records(session, job_id, fmt, **kw)]  # type: ignore[arg-type]
            )

    async def test_jsonl_is_one_object_per_line(self, maker) -> None:  # type: ignore[no-untyped-def]
        import json

        job_id = await self.seed_records(maker, [{"a": 1}, {"a": 2}])
        lines = (await self.collect(maker, job_id, "jsonl")).splitlines()
        assert [json.loads(line)["a"] for line in lines] == [1, 2]

    async def test_csv_has_a_header(self, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await self.seed_records(maker, [{"title": "x", "price": 1}])
        first = (await self.collect(maker, job_id, "csv")).splitlines()[0]
        assert set(first.split(",")) == {"title", "price"}

    async def test_a_key_that_only_appears_late_is_still_a_column(self, maker) -> None:  # type: ignore[no-untyped-def]
        """The reason column discovery is a query over every row rather than a
        peek at the first. A file missing a column is worse than a slow
        header: it looks complete."""
        job_id = await self.seed_records(
            maker, [{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4, "rare": "here"}]
        )
        header = (await self.collect(maker, job_id, "csv")).splitlines()[0]
        assert "rare" in header

    async def test_a_list_value_survives_as_json(self, maker) -> None:  # type: ignore[no-untyped-def]
        """``str()`` on a Python list emits single quotes that no JSON parser
        accepts back, which would make the column unreadable."""
        job_id = await self.seed_records(maker, [{"tags": ["a", "b"]}])
        body = await self.collect(maker, job_id, "csv")
        assert '"[""a"", ""b""]"' in body or '[""a"", ""b""]' in body

    async def test_source_columns_are_appended_not_merged(self, maker) -> None:  # type: ignore[no-untyped-def]
        """A recipe can have a field called ``page_url`` of its own, and
        overwriting it would corrupt the export undetectably."""
        job_id = await self.seed_records(maker, [{"page_url": "the recipe's own"}])
        body = await self.collect(maker, job_id, "csv", include_source=True)
        header = body.splitlines()[0]
        assert "page_url" in header
        assert "_page_url" in header
        assert "the recipe's own" in body

    async def test_records_come_out_in_order(self, maker) -> None:  # type: ignore[no-untyped-def]
        """Keyset paging has to preserve insertion order across chunks, or a
        large export is silently shuffled."""
        import json

        from crwallm.services.export import CHUNK_ROWS

        job_id = await self.seed_records(maker, [{"n": n} for n in range(CHUNK_ROWS + 20)])
        lines = (await self.collect(maker, job_id, "jsonl")).splitlines()
        assert [json.loads(line)["n"] for line in lines] == list(range(CHUNK_ROWS + 20))

    async def test_paging_reaches_past_one_chunk(self, maker) -> None:  # type: ignore[no-untyped-def]
        """A cursor that never advances would return the first chunk forever
        or stop after it."""
        from crwallm.services.export import CHUNK_ROWS

        job_id = await self.seed_records(maker, [{"n": n} for n in range(CHUNK_ROWS * 2 + 5)])
        lines = (await self.collect(maker, job_id, "jsonl")).splitlines()
        assert len(lines) == CHUNK_ROWS * 2 + 5

    async def test_a_job_with_no_records_exports_a_header_only(self, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await submit(maker)
        assert await self.collect(maker, job_id, "jsonl") == ""
        assert (await self.collect(maker, job_id, "csv")).strip() == ""

    async def test_an_unknown_format_is_refused(self, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await submit(maker)
        with pytest.raises(ValueError, match="unknown export format"):
            await self.collect(maker, job_id, "parquet")
