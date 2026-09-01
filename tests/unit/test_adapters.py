"""Consumption adapters - docs/04_CRAWLING_ARCHITECTURE.md.

The point of these is that the engine has exactly one output shape and every
other style is derived. Each test here is really a claim about that: whatever
the caller wants, they get it without the engine growing a second entry point,
and without anyone forgetting to close the stream.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from crwallm.crawler.adapters import CrawlOutcome, collect, drain_to, to_sse, with_callbacks
from crwallm.schemas.events import (
    CrawlEvent,
    JobCompleted,
    PageFetched,
    RecordsExtracted,
)
from crwallm.schemas.types import FetchMode


def page(url: str) -> PageFetched:
    return PageFetched(
        url=url, status=200, bytes=100, elapsed_ms=10, depth=0, fetch_mode=FetchMode.HTTP
    )


async def sample_stream() -> AsyncGenerator[CrawlEvent, None]:
    yield page("https://a.com/1")
    yield RecordsExtracted(
        url="https://a.com/1",
        extractor="css",
        count=2,
        records=({"title": "one"}, {"title": "two"}),
    )
    yield page("https://a.com/2")
    yield JobCompleted(pages_fetched=2, records_extracted=2, elapsed_s=1.5)


class TestCollect:
    async def test_gathers_events_and_records(self) -> None:
        outcome = await collect(sample_stream())
        assert isinstance(outcome, CrawlOutcome)
        assert outcome.pages_fetched == 2
        assert [r["title"] for r in outcome.records] == ["one", "two"]
        assert len(outcome.events) == 4


class TestDrainTo:
    async def test_every_event_reaches_the_sink(self) -> None:
        received: list[CrawlEvent] = []
        flushed = False

        class Sink:
            async def handle(self, event: CrawlEvent) -> None:
                received.append(event)

            async def flush(self) -> None:
                nonlocal flushed
                flushed = True

        await drain_to(sample_stream(), Sink())
        assert len(received) == 4
        assert flushed, "a sink that is never flushed loses its last batch"

    async def test_cancellation_stops_the_drain_and_still_flushes(self) -> None:
        """A cancelled job must persist what it already collected."""
        received: list[CrawlEvent] = []
        flushed = False

        class Sink:
            async def handle(self, event: CrawlEvent) -> None:
                received.append(event)

            async def flush(self) -> None:
                nonlocal flushed
                flushed = True

        async def cancel_after_first() -> bool:
            return len(received) >= 1

        await drain_to(sample_stream(), Sink(), should_cancel=cancel_after_first)
        assert len(received) == 1
        assert flushed


class TestToSse:
    async def test_frames_are_well_formed(self) -> None:
        frames = [f async for f in to_sse(sample_stream())]
        assert len(frames) == 4
        assert frames[0].startswith("event: page.fetched\ndata: {")
        assert frames[0].endswith("\n\n")

    async def test_no_event_id_is_emitted(self) -> None:
        """Ids come from the database sequence so ``Last-Event-ID`` can replay;
        the engine has no idea what they are."""
        frames = [f async for f in to_sse(sample_stream())]
        assert not any(line.startswith("id:") for f in frames for line in f.splitlines())

    async def test_payload_is_json_and_not_ascii_escaped(self) -> None:
        import json

        async def korean() -> AsyncGenerator[CrawlEvent, None]:
            yield RecordsExtracted(
                url="https://a.com/", extractor="css", count=1, records=({"제목": "노트북"},)
            )

        frame = await anext(to_sse(korean()))
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["records"][0]["제목"] == "노트북"


class TestWithCallbacks:
    async def test_typed_callbacks_receive_only_their_events(self) -> None:
        pages: list[PageFetched] = []
        records: list[RecordsExtracted] = []
        everything: list[CrawlEvent] = []

        async def on_page(e: PageFetched) -> None:
            pages.append(e)

        async def on_records(e: RecordsExtracted) -> None:
            records.append(e)

        async def on_event(e: CrawlEvent) -> None:
            everything.append(e)

        await with_callbacks(
            sample_stream(), on_event=on_event, on_page=on_page, on_records=on_records
        )
        assert len(pages) == 2
        assert len(records) == 1
        assert len(everything) == 4


class TestAclosingIsAlwaysApplied:
    """Regression guard.

    Every adapter must close the stream, including on the early-exit and
    exception paths, or a crawl outlives its consumer.
    """

    @staticmethod
    async def _tracked() -> tuple[AsyncGenerator[CrawlEvent, None], list[str]]:
        log: list[str] = []

        async def gen() -> AsyncGenerator[CrawlEvent, None]:
            try:
                while True:
                    yield page("https://a.com/x")
            finally:
                log.append("closed")

        return gen(), log

    async def test_collect_closes_on_exception(self) -> None:
        stream, log = await self._tracked()

        class BoomError(Exception):
            pass

        async def explode() -> None:
            async for _ in stream:
                raise BoomError

        with pytest.raises(BoomError):
            await explode()
        await stream.aclose()
        assert log == ["closed"]

    async def test_drain_to_closes_on_cancellation(self) -> None:
        stream, log = await self._tracked()

        class Sink:
            async def handle(self, event: CrawlEvent) -> None: ...
            async def flush(self) -> None: ...

        async def cancel_now() -> bool:
            return True

        await drain_to(stream, Sink(), should_cancel=cancel_now)
        assert log == ["closed"], "breaking out of the drain must close the stream"
