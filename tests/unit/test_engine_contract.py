"""EventPump lifecycle - docs/04_CRAWLING_ARCHITECTURE.md.

The generator shape is only safe if abandoning the stream actually stops the
workers. A leak here is invisible: the crawl keeps running, keeps fetching and
keeps consuming the host's patience, while the caller has moved on. So the
teardown paths are tested directly rather than assumed.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from crwallm.crawler.engine import EventPump
from crwallm.schemas.events import Progress
from crwallm.schemas.types import FetchMode


def progress(i: int) -> Progress:
    return Progress(pages_done=i, pages_queued=0, records_total=0)


class TestOrdering:
    async def test_events_arrive_in_emission_order(self) -> None:
        pump = EventPump()

        async def producer() -> None:
            for i in range(5):
                await pump.emit(progress(i))
            await pump.finish()

        pump.spawn(producer())
        seen = [e.pages_done async for e in pump.stream()]  # type: ignore[attr-defined]
        assert seen == [0, 1, 2, 3, 4]

    async def test_many_workers_fan_into_one_stream(self) -> None:
        pump = EventPump()
        workers = 8
        per_worker = 10
        done = asyncio.Event()

        async def worker(_: int) -> None:
            for _i in range(per_worker):
                await pump.emit(progress(0))

        async def closer() -> None:
            await done.wait()
            await pump.finish()

        for w in range(workers):
            pump.spawn(worker(w))
        pump.spawn(closer())

        async def run() -> int:
            count = 0
            async for _ in pump.stream():
                count += 1
                if count == workers * per_worker:
                    done.set()
            return count

        assert await run() == workers * per_worker


class TestBackpressure:
    async def test_producer_blocks_when_the_consumer_stalls(self) -> None:
        """A bounded queue is the whole backpressure mechanism. Without it a
        fast crawl accumulates the entire result set in memory."""
        pump = EventPump(queue_size=2)
        emitted = 0

        async def producer() -> None:
            nonlocal emitted
            for i in range(10):
                await pump.emit(progress(i))
                emitted += 1
            await pump.finish()

        pump.spawn(producer())
        await asyncio.sleep(0.02)

        # queue holds 2, one may be in flight - nowhere near 10.
        assert emitted <= 4, f"producer ran ahead ({emitted}) - queue is unbounded"

        drained = [e async for e in pump.stream()]
        assert len(drained) == 10


class TestCancellation:
    async def test_aclosing_stops_the_workers(self) -> None:
        """The supported way to stop early.

        ``contextlib.aclosing`` closes the generator, the ``finally`` in
        ``stream`` runs, and the pool is torn down before we return.
        """
        pump = EventPump()
        ran = 0

        async def forever() -> None:
            nonlocal ran
            while True:
                ran += 1
                await pump.emit(progress(ran))

        pump.spawn(forever(), name="forever")

        async with aclosing(pump.stream()) as stream:
            async for _ in stream:
                break

        assert pump.active_workers == 0, "workers survived the consumer"

        checkpoint = ran
        await asyncio.sleep(0.02)
        assert ran == checkpoint, "worker is still crawling after teardown"

    async def test_bare_break_does_not_close_the_generator(self) -> None:
        """Documents the trap, so nobody 'fixes' the adapters by removing
        ``aclosing``.

        Breaking out of an ``async for`` leaves the generator suspended; async
        generators are finalised by the loop's hooks at collection time, not at
        ``break``. The crawl keeps running and the caller cannot tell.

        The generator is held in a local and checked immediately. The first
        version dropped the reference and slept, which handed the collector the
        very window the claim is about: on Linux it ran the finaliser inside
        that sleep and the assertion failed, on Windows it did not and the test
        passed. Both were describing garbage-collection timing rather than the
        contract, and the contract is that ``break`` alone tears nothing down.
        """
        pump = EventPump()
        ran = 0

        async def forever() -> None:
            nonlocal ran
            while True:
                ran += 1
                await pump.emit(progress(ran))

        pump.spawn(forever())

        stream = pump.stream()
        async for _ in stream:
            break

        assert pump.active_workers == 1, (
            "bare break now tears down - if this became true on purpose, "
            "update the adapters and this test together"
        )

        # And the worker is not merely alive, it is still crawling - which is
        # the half of the trap that costs somebody a bill.
        checkpoint = ran
        await asyncio.sleep(0.02)
        assert ran > checkpoint, "worker stopped without anyone closing the stream"

        await stream.aclose()
        await pump.aclose()

    async def test_explicit_aclose_is_idempotent(self) -> None:
        pump = EventPump()

        async def forever() -> None:
            while True:
                await pump.emit(progress(0))

        pump.spawn(forever())
        await pump.aclose()
        await pump.aclose()
        assert pump.active_workers == 0

    async def test_consumer_exception_tears_down(self) -> None:
        pump = EventPump()

        async def forever() -> None:
            while True:
                await pump.emit(progress(0))

        pump.spawn(forever())

        with pytest.raises(RuntimeError, match="consumer blew up"):
            async with aclosing(pump.stream()) as stream:
                async for _ in stream:
                    raise RuntimeError("consumer blew up")

        assert pump.active_workers == 0


class TestWorkerFailure:
    async def test_worker_exception_reaches_the_consumer(self) -> None:
        """A crawl that dies must not look like a crawl that finished early."""
        pump = EventPump()

        async def broken() -> None:
            await pump.emit(progress(1))
            await pump.finish()
            raise ValueError("fetcher exploded")

        pump.spawn(broken())

        with pytest.raises(ValueError, match="fetcher exploded"):
            async for _ in pump.stream():
                pass

    async def test_first_failure_is_the_one_reported(self) -> None:
        pump = EventPump()

        async def first() -> None:
            raise ValueError("first")

        async def second() -> None:
            await asyncio.sleep(0.01)
            raise ValueError("second")

        async def closer() -> None:
            await asyncio.sleep(0.02)
            await pump.finish()

        pump.spawn(first())
        pump.spawn(second())
        pump.spawn(closer())

        with pytest.raises(ValueError, match="first"):
            async for _ in pump.stream():
                pass


class TestCrawlSignature:
    async def test_crawl_is_an_async_generator_pending_phase_2(self) -> None:
        """The signature is frozen now because every layer above is written
        against it; the traversal itself is Phase 2."""
        import inspect

        from crwallm.crawler.engine import crawl

        assert inspect.isasyncgenfunction(crawl)

        sig = inspect.signature(crawl)
        assert list(sig.parameters) == ["spec", "fetcher", "frontier", "gate", "on_cancel"]
        for name in ("fetcher", "frontier", "gate", "on_cancel"):
            assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


class TestEventContract:
    def test_events_carry_no_job_id(self) -> None:
        """The engine does not know what a job is - the sink attaches that.

        docs/04_CRAWLING_ARCHITECTURE.md
        """
        from crwallm.schemas.events import PageFetched

        assert "job_id" not in PageFetched.model_fields

    def test_events_are_frozen(self) -> None:
        from crwallm.schemas.events import PageFetched

        ev = PageFetched(
            url="https://a.com/",
            status=200,
            bytes=1,
            elapsed_ms=1,
            depth=0,
            fetch_mode=FetchMode.HTTP,
        )
        with pytest.raises(Exception):  # pydantic raises ValidationError
            ev.status = 500  # type: ignore[misc]

    def test_union_discriminates_on_type(self) -> None:
        from pydantic import TypeAdapter

        from crwallm.schemas.events import CrawlEvent, UrlRejected

        raw = UrlRejected(url="https://a.com/x", reason="ssrf").model_dump_json()  # type: ignore[arg-type]
        back = TypeAdapter(CrawlEvent).validate_json(raw)
        assert isinstance(back, UrlRejected)
