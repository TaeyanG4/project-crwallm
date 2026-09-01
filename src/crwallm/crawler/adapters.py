"""Consumption styles, built on the one primitive.

``crawl`` yields events. Everything else is shaped from that here, so the
engine never grows a second entry point (docs/04_CRAWLING_ARCHITECTURE.md).

Every adapter wraps the stream in ``contextlib.aclosing``. That is not
decoration: breaking out of an ``async for`` does not close an async
generator, so a consumer that stops early would otherwise leave the worker
pool crawling until garbage collection got round to it. Callers should use
these rather than iterating ``crawl`` by hand.

The parameters are typed ``AsyncGenerator`` rather than ``AsyncIterator`` for
the same reason: only a generator can be closed, so the requirement belongs in
the signature where the type checker can hold us to it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Protocol

from crwallm.schemas.events import CrawlEvent, PageFetched, RecordsExtracted

__all__ = ["CrawlOutcome", "EventSink", "collect", "drain_to", "to_sse"]


class EventSink(Protocol):
    """Where a worker persists events. Implemented in Phase 2 over PostgreSQL."""

    async def handle(self, event: CrawlEvent) -> None: ...

    async def flush(self) -> None: ...


@dataclass(slots=True)
class CrawlOutcome:
    """Everything a small crawl produced, in memory.

    For CLI runs and previews. Not for a spider - use a sink there, or the
    whole crawl ends up on the heap.
    """

    events: list[CrawlEvent] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    pages_fetched: int = 0


async def collect(stream: AsyncGenerator[CrawlEvent, None]) -> CrawlOutcome:
    """Drain a crawl into memory."""
    outcome = CrawlOutcome()
    async with aclosing(stream) as events:
        async for event in events:
            outcome.events.append(event)
            if isinstance(event, PageFetched):
                outcome.pages_fetched += 1
            elif isinstance(event, RecordsExtracted):
                outcome.records.extend(event.records)
    return outcome


async def drain_to(
    stream: AsyncGenerator[CrawlEvent, None],
    sink: EventSink,
    *,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """Persist a crawl. The worker's path.

    ``should_cancel`` is polled between events so a cancellation request
    reaches the crawl without the engine knowing what a job is.
    """
    async with aclosing(stream) as events:
        async for event in events:
            await sink.handle(event)
            if should_cancel is not None and await should_cancel():
                break
    await sink.flush()


async def to_sse(stream: AsyncGenerator[CrawlEvent, None]) -> AsyncIterator[str]:
    """Server-sent events frames.

    ``id`` is deliberately absent: ids come from the database sequence so that
    ``Last-Event-ID`` can replay, and only the sink knows them.
    """
    async with aclosing(stream) as events:
        async for event in events:
            payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
            yield f"event: {event.type}\ndata: {payload}\n\n"


async def with_callbacks(
    stream: AsyncGenerator[CrawlEvent, None],
    *,
    on_event: Callable[[CrawlEvent], Awaitable[None]] | None = None,
    on_page: Callable[[PageFetched], Awaitable[None]] | None = None,
    on_records: Callable[[RecordsExtracted], Awaitable[None]] | None = None,
) -> None:
    """Callback style, for callers who prefer it. Three lines, because the
    generator converts this way and not the other."""
    async with aclosing(stream) as events:
        async for event in events:
            if on_event is not None:
                await on_event(event)
            if on_page is not None and isinstance(event, PageFetched):
                await on_page(event)
            if on_records is not None and isinstance(event, RecordsExtracted):
                await on_records(event)
