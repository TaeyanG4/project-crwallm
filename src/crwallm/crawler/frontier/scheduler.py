"""Host-partitioned priority frontier.

Replaces the Phase 2 FIFO. Two problems it solves, and they turn out to be the
same problem seen from opposite sides:

**A single queue serialises on one host.** Breadth-first order groups a site's
links together, so a plain FIFO hands one worker after another the same
hostname. That is simultaneously the fastest way to get blocked and the
slowest way to crawl, because every other host sits idle while one is
hammered. Round-robin across per-host queues fixes both at once: a hundred
hosts in parallel is faster *and* gentler than one host at a hundred times the
rate (docs/05_SPIDER_ARCHITECTURE.md).

**Pure breadth spends the budget on the wrong pages.** Depth ordering treats a
product page and a terms-of-service link as equally interesting. Scoring by
path shape, depth and where a link was found puts the pages someone actually
wanted first, which matters precisely because the budget runs out.

Per-host politeness lives here too - not for etiquette, which this tool
relaxes, but because a host that starts refusing is a host that stops
producing data (docs/12_PERFORMANCE.md).
"""

from __future__ import annotations

import heapq
import itertools
import re
import time
from collections import deque
from dataclasses import dataclass, field

from crwallm.crawler.contracts import FrontierItem

__all__ = ["HostFrontier", "HostState", "score_url"]

# Paths that usually lead somewhere worth collecting, and paths that usually
# do not. Deliberately coarse: this orders a queue, it does not decide
# membership - that is the gate's job.
_PROMISING = re.compile(
    r"/(?:product|item|goods|article|post|news|story|job|listing|detail|view|p|read)"
    r"(?:/|$|\?)",
    re.IGNORECASE,
)
_UNPROMISING = re.compile(
    r"/(?:tag|tags|category|categories|archive|author|search|login|signin|signup|register"
    r"|cart|checkout|account|profile|settings|privacy|terms|policy|legal|help|faq"
    r"|contact|about|sitemap|feed|rss|print|share|comment|reply)"
    r"(?:/|$|\?)",
    re.IGNORECASE,
)


def score_url(
    url: str, depth: int, *, from_sitemap: bool = False, hint: float | None = None
) -> int:
    """Higher is crawled sooner.

    Integers rather than floats so ordering is exact and reproducible - a
    frontier that reorders itself between runs makes a truncated crawl
    impossible to compare against the previous one.
    """
    score = 1000 - depth * 100

    if from_sitemap:
        # The site listing a URL is the strongest signal available: it is
        # saying this is a page, not navigation furniture.
        score += 300
        if hint is not None:
            score += int(hint * 100)

    if _PROMISING.search(url):
        score += 200
    if _UNPROMISING.search(url):
        score -= 400

    # Long query strings are usually faceted navigation, which is a
    # combinatorial hole even after the trap guards bound it.
    score -= url.count("&") * 20
    return score


@dataclass(slots=True)
class HostState:
    """One host's queue and its pace.

    ``next_allowed_at`` is the whole of politeness here: a host that answered
    429 is not asked again until it said we could, and everything else keeps
    running in the meantime.
    """

    host: str
    queue: list[tuple[int, int, FrontierItem]] = field(default_factory=list)
    in_flight: int = 0
    next_allowed_at: float = 0.0
    fetched: int = 0
    failures: int = 0

    @property
    def pending(self) -> int:
        return len(self.queue)

    @property
    def idle(self) -> bool:
        return not self.queue and self.in_flight == 0

    def ready_at(self, now: float) -> bool:
        return now >= self.next_allowed_at

    def defer(self, seconds: float) -> None:
        self.next_allowed_at = max(self.next_allowed_at, time.monotonic() + seconds)


@dataclass(slots=True)
class HostFrontier:
    """Priority queue per host, round-robin between hosts.

    Substitutable for the Phase 2 ``MemoryFrontier`` - the engine sees only
    the ``Frontier`` protocol, which is why this could be swapped in without
    the traversal loop changing.
    """

    per_host_concurrency: int = 8
    min_interval_s: float = 0.0
    max_hosts: int = 10_000

    _hosts: dict[str, HostState] = field(default_factory=dict)
    _rotation: deque[str] = field(default_factory=deque)
    _seen: set[str] = field(default_factory=set)
    _counter: itertools.count[int] = field(default_factory=itertools.count)
    _in_flight: int = 0
    _bloom: object | None = None

    # ------------------------------------------------------------ dedupe

    def _remember(self, key: str) -> bool:
        """``True`` if this key is new.

        An exact set until it gets big, then a Bloom filter behind it. A
        million URLs in a Python set is a few hundred megabytes; the filter is
        a few. False positives drop a URL that was never seen, which on a
        crawl of that size is an acceptable trade and a tunable one.
        """
        if key in self._seen:
            return False

        if self._bloom is not None and key in self._bloom:  # type: ignore[operator]
            return False

        if len(self._seen) >= 1_000_000:
            if self._bloom is None:
                from rbloom import Bloom

                self._bloom = Bloom(50_000_000, 0.001)
            self._bloom.add(key)  # type: ignore[attr-defined]
        else:
            self._seen.add(key)
        return True

    # ----------------------------------------------------------- protocol

    async def add(self, item: FrontierItem) -> bool:
        key = item.url.dedupe_key
        if not self._remember(key):
            return False

        host = item.url.host
        state = self._hosts.get(host)
        if state is None:
            if len(self._hosts) >= self.max_hosts:
                # A spider that has found ten thousand hosts has left the site
                # it was pointed at. Refusing new ones bounds memory without
                # abandoning the ones already in progress.
                return False
            state = HostState(host=host)
            self._hosts[host] = state
            self._rotation.append(host)

        # Negated priority: heapq is a min-heap, and the counter breaks ties
        # in insertion order so equal-scoring URLs keep a stable sequence.
        heapq.heappush(state.queue, (-item.priority, next(self._counter), item))
        return True

    async def next(self) -> FrontierItem | None:
        """The next item from the next eligible host.

        Rotates rather than scanning: whichever host comes up first and is
        ready gets served, and it goes to the back. That is what keeps one
        busy host from starving the rest.
        """
        now = time.monotonic()

        for _ in range(len(self._rotation)):
            host = self._rotation[0]
            self._rotation.rotate(-1)

            state = self._hosts.get(host)
            if state is None or not state.queue:
                continue
            if state.in_flight >= self.per_host_concurrency:
                continue
            if not state.ready_at(now):
                continue

            _, _, item = heapq.heappop(state.queue)
            state.in_flight += 1
            self._in_flight += 1
            if self.min_interval_s:
                state.defer(self.min_interval_s)
            return item

        return None

    async def done(self, item: FrontierItem) -> None:
        state = self._hosts.get(item.url.host)
        if state is not None:
            state.in_flight = max(0, state.in_flight - 1)
            state.fetched += 1
        self._in_flight = max(0, self._in_flight - 1)

    # -------------------------------------------------------------- state

    @property
    def pending(self) -> int:
        return sum(len(s.queue) for s in self._hosts.values())

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def seen(self) -> int:
        return len(self._seen)

    @property
    def exhausted(self) -> bool:
        """Nothing queued and nothing being worked on.

        An empty queue alone does not mean finished: a worker holding a page
        is about to discover a hundred more links.
        """
        return self.pending == 0 and self._in_flight == 0

    @property
    def hosts_active(self) -> int:
        return sum(1 for s in self._hosts.values() if not s.idle)

    def mark_seen(self, dedupe_key: str) -> None:
        self._remember(dedupe_key)

    def has_seen(self, dedupe_key: str) -> bool:
        if dedupe_key in self._seen:
            return True
        return self._bloom is not None and dedupe_key in self._bloom  # type: ignore[operator]

    # ---------------------------------------------------------- pacing

    def penalise(self, host: str, seconds: float) -> None:
        """Stand off from a host that pushed back.

        Called on 429 and 403. This is not politeness in the etiquette sense -
        it is that a blocked host produces nothing, so backing off is the
        faster path to the data (docs/12_PERFORMANCE.md).
        """
        state = self._hosts.get(host)
        if state is not None:
            state.failures += 1
            state.defer(seconds)

    def stats(self) -> dict[str, dict[str, int]]:
        return {
            host: {
                "pending": state.pending,
                "fetched": state.fetched,
                "failures": state.failures,
            }
            for host, state in sorted(self._hosts.items())
            if state.fetched or state.pending
        }
