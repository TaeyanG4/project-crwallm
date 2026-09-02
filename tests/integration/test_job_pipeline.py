"""Submit, claim, crawl, persist - against a real PostgreSQL.

The unit tests cover each piece in isolation. What only a database shows is
whether the pieces agree: whether the migration matches the models, whether
``SKIP LOCKED`` really lets two workers share a queue, whether the sink's
``ON CONFLICT`` constraint exists under the name it uses.

Skipped when no database is reachable, so the suite still runs on a machine
without Docker. That is a deliberate trade: these are the tests most worth
having and the ones most awkward to require.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from crwallm.crawler.adapters import drain_to
from crwallm.crawler.extraction.css import CssExtractor, CssSpec, FieldSpec
from crwallm.crawler.fetching.http import SafeHttpFetcher
from crwallm.crawler.frontier.memory import MemoryFrontier
from crwallm.crawler.traversal import CrawlDeps, run_crawl
from crwallm.db.models import CrawlEventRow, CrawlResult, ExtractedRecord, JobStatus
from crwallm.jobs.sink import PostgresEventSink, record_hash
from crwallm.policy.gate import UrlGate
from crwallm.policy.ssrf import SsrfGuard, StaticResolver
from crwallm.schemas.spec import CrawlLimits, CrawlSpec
from crwallm.services.job import JobService
from crwallm.storage.blob import NullBlobStore
from tests.fixtures.malicious_server.server import MaliciousServer, RunningServer

pytestmark = pytest.mark.integration

LOOPBACK = [ipaddress.ip_network("127.0.0.0/8")]

TEST_DATABASE_URL = os.environ.get(
    "CRWALLM_TEST_DATABASE_URL",
    "postgresql+asyncpg://crwallm:crwallm@localhost:5432/crwallm_test",
)


async def _database_reachable() -> bool:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
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


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest.fixture(scope="module")
def server() -> Iterator[RunningServer]:
    s = MaliciousServer()
    try:
        yield s.start()
    finally:
        s.stop()


class _LoopbackScope:
    def contains(self, host: str) -> bool:
        return host.strip().lower() == "127.0.0.1"


def make_spec(server: RunningServer, path: str = "/", **kw: object) -> CrawlSpec:
    return CrawlSpec(
        seed_urls=(server.url(path),),
        allowed_domains=("127.0.0.1",),
        limits=CrawlLimits(max_pages=5, global_concurrency=2, **kw),  # type: ignore[arg-type]
    )


class TestSchemaMatchesModels:
    async def test_every_table_exists(self, session: AsyncSession) -> None:
        for table in (
            "crawl_specs",
            "crawl_jobs",
            "crawl_results",
            "extracted_records",
            "crawl_events",
        ):
            result = await session.execute(
                text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
            )
            assert result.scalar() is not None, f"{table} is missing"

    async def test_the_record_identity_constraint_exists_by_name(
        self, session: AsyncSession
    ) -> None:
        """The sink names this constraint in its ON CONFLICT clause. If it is
        renamed, every record write fails at runtime and nowhere else."""
        result = await session.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = 'record_identity'")
        )
        assert result.scalar() == 1


class TestQueue:
    async def test_submit_then_claim(self, session: AsyncSession, server: RunningServer) -> None:
        jobs = JobService(session)
        job = await jobs.submit(make_spec(server))
        assert job.status == JobStatus.QUEUED

        claimed = await jobs.claim("worker-1")
        assert claimed is not None
        got, spec = claimed
        assert got.id == job.id
        assert got.status == JobStatus.RUNNING
        assert got.worker_id == "worker-1"
        assert spec.seed_urls == make_spec(server).seed_urls

    async def test_an_empty_queue_returns_nothing(self, session: AsyncSession) -> None:
        assert await JobService(session).claim("worker-1") is None

    async def test_two_workers_never_claim_the_same_job(
        self,
        engine,
        server: RunningServer,  # type: ignore[no-untyped-def]
    ) -> None:
        """What SKIP LOCKED buys: concurrent claims take different rows rather
        than queueing behind the same one."""
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            for _ in range(4):
                await JobService(s).submit(make_spec(server))

        claimed_ids = []
        for name in ("w1", "w2", "w3", "w4"):
            async with maker() as s:
                got = await JobService(s).claim(name)
                assert got is not None
                claimed_ids.append(got[0].id)

        assert len(set(claimed_ids)) == 4

    async def test_priority_is_honoured(
        self,
        engine,
        server: RunningServer,  # type: ignore[no-untyped-def]
    ) -> None:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            await JobService(s).submit(make_spec(server), priority=0)
            urgent = await JobService(s).submit(make_spec(server), priority=10)

        async with maker() as s:
            claimed = await JobService(s).claim("w1")
            assert claimed is not None
            assert claimed[0].id == urgent.id


class TestSinkPersistence:
    async def test_a_crawl_lands_in_the_database(
        self, session: AsyncSession, server: RunningServer
    ) -> None:
        jobs = JobService(session)
        spec = make_spec(server, "/calendar/2031/07")
        job = await jobs.submit(spec)

        guard = SsrfGuard(StaticResolver({}), allow_networks=LOOPBACK)  # type: ignore[arg-type]
        fetcher = SafeHttpFetcher(guard, http2=False)
        deps = CrawlDeps(
            fetcher=fetcher,
            frontier=MemoryFrontier(),
            gate=UrlGate.build(spec, guard, scope=_LoopbackScope()),  # type: ignore[arg-type]
            extractor=CssExtractor(
                CssSpec(fields=(FieldSpec("heading", "h1", "text"),), follow_links=False)
            ),
            archive=NullBlobStore(),
        )
        sink = PostgresEventSink(session=session, job_id=job.id)
        try:
            await drain_to(run_crawl(spec, deps), sink)
        finally:
            await fetcher.aclose()

        refreshed = await jobs.get(job.id)
        assert refreshed is not None
        assert refreshed.status == JobStatus.COMPLETED
        assert refreshed.pages_crawled == 1
        assert refreshed.completed_at is not None

        results = (
            (await session.execute(select(CrawlResult).where(CrawlResult.job_id == job.id)))
            .scalars()
            .all()
        )
        assert len(results) == 1

        records = (
            (await session.execute(select(ExtractedRecord).where(ExtractedRecord.job_id == job.id)))
            .scalars()
            .all()
        )
        assert [r.data["heading"] for r in records] == ["2031-07"]

        events = (
            (
                await session.execute(
                    select(CrawlEventRow)
                    .where(CrawlEventRow.job_id == job.id)
                    .order_by(CrawlEventRow.id)
                )
            )
            .scalars()
            .all()
        )
        assert events[0].event_type == "job.started"
        assert events[-1].event_type == "job.completed"

    async def test_record_payloads_are_not_stored_twice(
        self, session: AsyncSession, server: RunningServer
    ) -> None:
        """The event log keeps counts; the rows live in extracted_records.
        Storing both would duplicate the entire result set."""
        job = await JobService(session).submit(make_spec(server))
        sink = PostgresEventSink(session=session, job_id=job.id)

        from crwallm.schemas.events import RecordsExtracted

        await sink.handle(
            RecordsExtracted(
                url="https://a.test/",
                extractor="css",
                count=2,
                records=({"a": 1}, {"a": 2}),
            )
        )
        await sink.flush()

        event = (
            (
                await session.execute(
                    select(CrawlEventRow).where(CrawlEventRow.event_type == "records.extracted")
                )
            )
            .scalars()
            .first()
        )
        assert event is not None
        assert "records" not in event.payload
        assert event.payload["count"] == 2

    async def test_rerunning_a_page_does_not_duplicate_rows(
        self, session: AsyncSession, server: RunningServer
    ) -> None:
        """What makes Phase 8's resume safe: the same page produces the same
        hashes, and the unique constraint absorbs the repeat."""
        from crwallm.schemas.events import RecordsExtracted

        job = await JobService(session).submit(make_spec(server))
        event = RecordsExtracted(
            url="https://a.test/p", extractor="css", count=1, records=({"title": "x"},)
        )

        for _ in range(3):
            sink = PostgresEventSink(session=session, job_id=job.id)
            await sink.handle(event)
            await sink.flush()

        rows = (
            (await session.execute(select(ExtractedRecord).where(ExtractedRecord.job_id == job.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].record_hash == record_hash("https://a.test/p", {"title": "x"})

    async def test_failure_tallies_are_written(
        self, session: AsyncSession, server: RunningServer
    ) -> None:
        """ "4 pages failed" is not actionable; the breakdown is."""
        from crwallm.schemas.events import PageFailed
        from crwallm.schemas.types import ErrorKind

        job = await JobService(session).submit(make_spec(server))
        sink = PostgresEventSink(session=session, job_id=job.id)
        for kind in (
            ErrorKind.BLOCKED_429,
            ErrorKind.BLOCKED_429,
            ErrorKind.READ_TIMEOUT,
        ):
            await sink.handle(
                PageFailed(url="https://a.test/x", depth=0, error_kind=kind, retryable=True)
            )
        await sink.flush()

        refreshed = await JobService(session).get(job.id)
        assert refreshed is not None
        assert refreshed.error_counts == {"blocked_429": 2, "read_timeout": 1}
        assert refreshed.pages_failed == 3


class TestWorker:
    async def test_the_worker_runs_a_queued_job_end_to_end(
        self,
        engine,  # type: ignore[no-untyped-def]
        server: RunningServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole Phase 2 path: submit, claim, crawl, persist, complete."""
        import functools

        from crwallm.jobs import worker as worker_mod

        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(worker_mod, "get_sessionmaker", lambda: maker)

        # The worker builds a production crawl, which refuses loopback. Point
        # it at the fixture through open_crawl's own parameters rather than
        # patching the guard's internals.
        monkeypatch.setattr(
            worker_mod,
            "open_crawl",
            functools.partial(
                worker_mod.open_crawl,
                guard=SsrfGuard(StaticResolver({}), allow_networks=LOOPBACK),  # type: ignore[arg-type]
                scope=_LoopbackScope(),  # type: ignore[arg-type]
            ),
        )

        async with maker() as s:
            job = await JobService(s).submit(make_spec(server, "/calendar/2031/07"))

        w = worker_mod.Worker()
        assert await w.run_once() is True

        async with maker() as s:
            refreshed = await JobService(s).get(job.id)
            assert refreshed is not None
            assert refreshed.status == JobStatus.COMPLETED
            assert refreshed.pages_crawled >= 1
            assert refreshed.worker_id == w.id
            assert refreshed.completed_at is not None

    async def test_an_empty_queue_is_reported_as_such(
        self,
        engine,  # type: ignore[no-untyped-def]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from crwallm.jobs import worker as worker_mod

        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(worker_mod, "get_sessionmaker", lambda: maker)
        assert await worker_mod.Worker().run_once() is False

    async def test_a_job_that_explodes_is_marked_failed_not_left_running(
        self,
        engine,  # type: ignore[no-untyped-def]
        server: RunningServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One bad job must not take the worker down, and must not leave a row
        that is indistinguishable from a slow crawl."""
        from crwallm.jobs import worker as worker_mod

        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(worker_mod, "get_sessionmaker", lambda: maker)

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("fetcher exploded")

        monkeypatch.setattr(worker_mod, "open_crawl", explode)

        async with maker() as s:
            job = await JobService(s).submit(make_spec(server))

        assert await worker_mod.Worker().run_once() is True

        async with maker() as s:
            refreshed = await JobService(s).get(job.id)
            assert refreshed is not None
            assert refreshed.status == JobStatus.FAILED
            assert "fetcher exploded" in (refreshed.error_message or "")
