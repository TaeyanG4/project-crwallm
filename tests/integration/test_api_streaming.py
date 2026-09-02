"""The endpoints the web UI reads: pages, event log, and the live stream.

The stream is the one worth testing against a real database. Its correctness
argument is about transaction visibility - the worker commits a batch of
events and the job's terminal status together, so the reader has to check the
status *before* it reads the events or it can end the stream having dropped
the batch that arrived in between. A stub session cannot exhibit that.

docs/09_JOB_ARCHITECTURE.md
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from crwallm.api.app import create_app
from crwallm.api.deps import session_dep, sessionmaker_dep
from crwallm.config import Settings
from crwallm.db.models import CrawlEventRow, CrawlJob, CrawlResult, CrawlSpecRow, JobStatus
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


async def seed_job(
    maker: async_sessionmaker[AsyncSession],
    *,
    status: str = JobStatus.COMPLETED,
    events: int = 3,
    pages: int = 2,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    spec_id = uuid.uuid4()
    async with maker() as s:
        s.add(
            CrawlSpecRow(
                id=spec_id,
                payload={
                    "seed_urls": ["https://shop.test/"],
                    "allowed_domains": ["shop.test"],
                },
            )
        )
        s.add(CrawlJob(id=job_id, spec_id=spec_id, status=status, priority=0))
        await s.flush()
        for i in range(events):
            s.add(
                CrawlEventRow(
                    job_id=job_id,
                    event_type="page.fetched",
                    payload={"type": "page.fetched", "url": f"https://shop.test/{i}"},
                )
            )
        for i in range(pages):
            s.add(
                CrawlResult(
                    id=uuid.uuid4(),
                    job_id=job_id,
                    url=f"https://shop.test/{i}",
                    status_code=200,
                    depth=i,
                    elapsed_ms=12,
                )
            )
        await s.commit()
    return job_id


@pytest_asyncio.fixture(loop_scope="module")
async def client(maker) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    """httpx against the ASGI app, not ``TestClient``.

    ``TestClient`` runs the app on a loop of its own, and the engine here
    belongs to the module's loop - asyncpg refuses to be driven across the
    two. Driving the app in-process keeps everything on one loop.

    The Host header is not decoration: the app answers 421 to anything not in
    the allowlist, which is what blocks DNS rebinding
    (docs/11_SECURITY_MODEL.md).
    """
    app = create_app(Settings(env="dev", api_token="t" * 32))

    async def override() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[session_dep] = override
    # The stream opens sessions of its own, so it needs the factory
    # pointed at the test engine too.
    app.dependency_overrides[sessionmaker_dep] = lambda: maker
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={"Host": "127.0.0.1"},
    ) as c:
        yield c


def frames(body: str) -> list[dict[str, object]]:
    """Parse an SSE body into its data payloads."""
    out = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


class TestPages:
    async def test_pages_show_what_was_fetched(self, client, maker) -> None:  # type: ignore[no-untyped-def]
        """A crawl reporting "200 pages, 0 records" is diagnosed here: all
        200s means the recipe missed, all 404s means the seeds were wrong."""
        job_id = await seed_job(maker, pages=4)
        body = (await client.get(f"/api/jobs/{job_id}/pages")).json()
        assert body["total_returned"] == 4
        assert body["pages"][0]["status_code"] == 200

    async def test_pages_404_for_an_unknown_job(self, client) -> None:  # type: ignore[no-untyped-def]
        assert (await client.get(f"/api/jobs/{uuid.uuid4()}/pages")).status_code == 404


class TestEventLog:
    async def test_the_log_reads_as_a_page(self, client, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await seed_job(maker, events=5)
        rows = (await client.get(f"/api/jobs/{job_id}/events")).json()
        assert len(rows) == 5
        assert rows[0]["id"] < rows[-1]["id"], "ordered by the cursor"

    async def test_after_skips_what_was_already_seen(self, client, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await seed_job(maker, events=5)
        rows = (await client.get(f"/api/jobs/{job_id}/events")).json()
        rest = (await client.get(f"/api/jobs/{job_id}/events?after={rows[1]['id']}")).json()
        assert len(rest) == 3


class TestStream:
    async def test_a_finished_job_streams_its_log_and_ends(self, client, maker) -> None:  # type: ignore[no-untyped-def]
        """No waiting: the job is already terminal, so the stream is a replay
        that terminates rather than a tail that hangs."""
        job_id = await seed_job(maker, events=3)
        body = (await client.get(f"/api/jobs/{job_id}/stream")).text
        assert len(frames(body)) == 4, "three events plus the end frame"
        assert "event: end" in body

    async def test_every_frame_carries_its_cursor(self, client, maker) -> None:  # type: ignore[no-untyped-def]
        job_id = await seed_job(maker, events=3)
        body = (await client.get(f"/api/jobs/{job_id}/stream")).text
        assert body.count("id: ") == 3, "every event frame is resumable"

    async def test_last_event_id_resumes_rather_than_replays(self, client, maker) -> None:  # type: ignore[no-untyped-def]
        """A dropped connection must not make the browser re-render the whole
        crawl from the beginning."""
        job_id = await seed_job(maker, events=5)
        rows = (await client.get(f"/api/jobs/{job_id}/events")).json()
        body = (
            await client.get(
                f"/api/jobs/{job_id}/stream",
                headers={"Last-Event-ID": str(rows[2]["id"])},
            )
        ).text
        assert len(frames(body)) == 3, "two remaining events plus end"

    async def test_the_header_wins_over_the_query_string(self, client, maker) -> None:  # type: ignore[no-untyped-def]
        """The browser's own idea of where it got to is better information
        than an ``after`` somebody assembled by hand."""
        job_id = await seed_job(maker, events=5)
        rows = (await client.get(f"/api/jobs/{job_id}/events")).json()
        body = (
            await client.get(
                f"/api/jobs/{job_id}/stream?after=0",
                headers={"Last-Event-ID": str(rows[3]["id"])},
            )
        ).text
        assert len(frames(body)) == 2, "one remaining event plus end"

    async def test_stream_404s_for_an_unknown_job(self, client) -> None:  # type: ignore[no-untyped-def]
        assert (await client.get(f"/api/jobs/{uuid.uuid4()}/stream")).status_code == 404
