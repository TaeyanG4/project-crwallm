"""In-memory BFS frontier.

Breadth-first because it makes budget spend predictable: everything at depth 1
is visited before anything at depth 2, so a crawl cut short by ``max_pages``
returns a shallow slice of the site rather than one deep tunnel. Depth-first
would spend the entire budget inside the first branch it happened to enter.

Deduplication is on ``dedupe_key``, never on the URL that gets fetched
(docs/05_SPIDER_ARCHITECTURE.md section 3). Two links that differ only by
``?utm_source=`` are one page and must occupy one slot; the fetch still uses
the URL the page actually offered.

Sized for Phase 2 - hundreds of thousands of URLs held in a set. Phase 5
replaces this with a host-partitioned priority queue persisted in PostgreSQL,
which is why the engine only ever sees the ``Frontier`` protocol.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from crwallm.crawler.contracts import FrontierItem

__all__ = ["MemoryFrontier"]


@dataclass(slots=True)
class MemoryFrontier:
    """FIFO queue plus a seen-set."""

    _queue: deque[FrontierItem] = field(default_factory=deque)
    _seen: set[str] = field(default_factory=set)
    _in_flight: int = 0

    async def add(self, item: FrontierItem) -> bool:
        """Enqueue unless the dedupe key is already known."""
        key = item.url.dedupe_key
        if key in self._seen:
            return False
        self._seen.add(key)
        self._queue.append(item)
        return True

    async def next(self) -> FrontierItem | None:
        if not self._queue:
            return None
        self._in_flight += 1
        return self._queue.popleft()

    async def done(self, item: FrontierItem) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    @property
    def pending(self) -> int:
        return len(self._queue)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def seen(self) -> int:
        return len(self._seen)

    @property
    def exhausted(self) -> bool:
        """Nothing queued and nothing being worked on.

        The queue being empty is not enough to stop: a worker still holding a
        page may be about to discover a hundred more links.
        """
        return not self._queue and self._in_flight == 0

    def mark_seen(self, dedupe_key: str) -> None:
        """Record a key without queueing it.

        Used for canonical URLs: once a page declares its canonical form, the
        alternate spellings should not be fetched even though nothing linked
        to them yet.
        """
        self._seen.add(dedupe_key)

    def has_seen(self, dedupe_key: str) -> bool:
        return dedupe_key in self._seen
