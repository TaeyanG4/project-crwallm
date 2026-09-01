"""The engine's public shape.

One primitive::

    async def crawl(spec, ...) -> AsyncGenerator[CrawlEvent, None]

Everything else - the DB sink, SSE, the CLI progress line, "just give me the
results" - is a thin adapter over this (``crwallm.crawler.adapters``).

**Why a generator.** Of the four plausible shapes (generator, callbacks, sink,
collected result) only the generator converts losslessly into the other three.
Generator to callback is three lines; callback to generator needs a queue, a
task, a sentinel and exception plumbing. Backpressure and cancellation also
come free: a consumer that stops pulling stops the crawl, and one that raises
propagates into the engine's ``finally``.

**Concurrency.** Hundreds of fetches run in parallel while events come out in
one ordered stream::

    fetch workers (N) ---> asyncio.Queue(maxsize) ---> generator drains -> yield

The bounded queue *is* the backpressure. When the consumer stalls the queue
fills, workers block on ``put`` and the crawl throttles itself.

**Cancellation, and the trap in it.** ``break`` out of an ``async for`` does
*not* close an async generator. Async generators are finalised by the event
loop's finaliser hooks whenever the object is collected, which for a crawl
means "some time later, maybe". A consumer that breaks out of the stream and
walks away therefore leaves the worker pool fetching - invisibly, because
everything still looks fine from the caller's side.

Deterministic teardown needs ``aclose()``, so the adapters in
``crwallm.crawler.adapters`` wrap every stream in ``contextlib.aclosing`` and
callers are expected to use them rather than iterating ``crawl`` directly.
``tests/unit/test_engine_contract.py`` asserts both halves: that ``aclosing``
stops the workers, and that bare ``break`` does not.

The traversal itself lands in Phase 2. This module fixes the signature that
Phase 2 fills in, because every layer above it is written against this shape.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING

from crwallm.schemas.events import CrawlEvent

if TYPE_CHECKING:
    from crwallm.crawler.contracts import Fetcher, Frontier, UrlGate
    from crwallm.schemas.spec import CrawlSpec

__all__ = ["EventPump", "crawl"]

DEFAULT_QUEUE_SIZE = 256
"""Bounded so a slow consumer throttles production rather than accumulating
the whole crawl in memory."""


class EventPump:
    """Fan-in from a worker pool to a single ordered event stream.

    Owns the lifecycle that is easy to leak: workers are cancelled and awaited
    in ``finally``, so abandoning the stream stops the crawl.
    """

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue: asyncio.Queue[CrawlEvent | None] = asyncio.Queue(maxsize=queue_size)
        self._workers: list[asyncio.Task[None]] = []
        self._failure: BaseException | None = None
        self._closing = False

    def spawn(self, coro: Awaitable[None], *, name: str | None = None) -> None:
        task = asyncio.ensure_future(coro)
        if name:
            task.set_name(name)
        task.add_done_callback(self._on_worker_done)
        self._workers.append(task)

    def _on_worker_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and self._failure is None:
            # Remember the first failure; it is re-raised out of the stream so
            # the consumer sees it rather than a silently short crawl.
            self._failure = exc

    async def emit(self, event: CrawlEvent) -> None:
        """Blocks while the consumer is behind. That is the backpressure."""
        await self._queue.put(event)

    async def finish(self) -> None:
        await self._queue.put(None)

    async def stream(self) -> AsyncGenerator[CrawlEvent, None]:
        try:
            while True:
                event = await self._queue.get()
                if event is None:
                    break
                yield event
            # Let workers settle before deciding the crawl succeeded. A worker
            # that raises immediately after signalling completion would
            # otherwise be reported as a short crawl rather than a failed one.
            await self._settle()
            if self._failure is not None:
                raise self._failure
        finally:
            await self.aclose()

    async def _settle(self, grace_s: float = 5.0) -> None:
        pending = [t for t in self._workers if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=grace_s)
        self._harvest_failures()

    def _harvest_failures(self) -> None:
        """Read exceptions off the tasks directly.

        ``add_done_callback`` schedules with ``call_soon``, so a worker that
        raises just after signalling completion is done but its callback has
        not run yet - and the crawl would be reported as successful. The
        callback still runs, and still wins, because it observes true failure
        order; this is the backstop for the ones it has not reached.
        """
        if self._failure is not None:
            return
        for task in self._workers:
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    self._failure = exc
                    return

    async def aclose(self) -> None:
        """Idempotent teardown. Runs on normal completion, on consumer
        ``break``/``aclose``, and on exception."""
        if self._closing:
            return
        self._closing = True
        for task in self._workers:
            if not task.done():
                task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    @property
    def active_workers(self) -> int:
        return sum(1 for t in self._workers if not t.done())


async def crawl(
    spec: CrawlSpec,
    *,
    fetcher: Fetcher,
    frontier: Frontier,
    gate: UrlGate,
    on_cancel: Callable[[], bool] | None = None,
) -> AsyncGenerator[CrawlEvent, None]:
    """Run ``spec`` and yield events as they happen.

    Dependencies are arguments, not imports: core must not know about httpx or
    PostgreSQL, and tests must be able to drive the whole loop with fakes.

    ``on_cancel`` is polled between pages so a worker process can honour a
    cancellation request without reaching into the engine.

    Phase 2 implements the traversal. The signature is fixed now because the
    service layer, the worker and the CLI are all written against it.
    """
    raise NotImplementedError("traversal lands in Phase 2 - see docs/16_ROADMAP.md")
    # Unreachable, and deliberately so: the yield is what makes this an async
    # generator function rather than a coroutine, so callers type-check against
    # the real shape before Phase 2 fills the body in.
    yield  # type: ignore[unreachable]
