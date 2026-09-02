"""The crawl loop.

Fills in what ``engine.crawl`` promised in Phase 1. One page's worth of work::

    frontier -> admit -> fetch -> archive -> extract -> discover -> frontier

Four things here are less obvious than they look.

**Stopping.** An empty queue does not mean the crawl is over: a worker holding
a page is about to discover a hundred more links. Termination is "queue empty
*and* nothing in flight", which is why the frontier tracks in-flight count and
not just length.

**Where the gate runs.** Twice, and each half exactly once per URL. Links are
gated on the way *into* the frontier (scope, filters, traps - all local), so a
spider never pays a DNS lookup to learn a link was off-site. SSRF and pinning
run on the way *out*, immediately before the request, because that is the only
point at which the check and the connection are the same event.

**Archiving before extraction.** The body is stored first and unconditionally.
Extraction changes across phases - Phase 6 adds JSON-LD, Phase 7 adds the
browser - and an archived body can be re-extracted, while a discarded one is
gone.

**Concurrency without disorder.** N workers run the loop; events reach the
consumer through one bounded queue, so ordering and backpressure hold without
the workers knowing anything about the consumer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from crwallm.crawler.contracts import (
    Extractor,
    Fetcher,
    FetchFailure,
    FetchRequest,
    FetchResponse,
    FrontierItem,
)
from crwallm.crawler.dedupe import ContentDeduper, SoftNotFoundDetector
from crwallm.crawler.engine import EventPump
from crwallm.crawler.frontier.memory import MemoryFrontier
from crwallm.crawler.frontier.scheduler import HostFrontier, score_url
from crwallm.policy.gate import UrlGate
from crwallm.policy.url import NormalizedUrl, UrlNormalizationError, normalize
from crwallm.schemas.events import (
    CrawlEvent,
    DuplicateDetected,
    JobCompleted,
    JobStarted,
    LinksDiscovered,
    PageFailed,
    PageFetched,
    PatternBudgetExhausted,
    Progress,
    RecordsExtracted,
    UrlRejected,
)
from crwallm.schemas.spec import CrawlSpec
from crwallm.schemas.types import ErrorKind, RejectReason
from crwallm.storage.blob import ArchiveRef, BlobStore, NullBlobStore

__all__ = ["CrawlDeps", "run_crawl"]

PROGRESS_EVERY = 10
"""Pages between progress events. Frequent enough to watch, rare enough that a
fast crawl is not dominated by them."""

_IDLE_SLEEP_S = 0.01


@dataclass(slots=True)
class CrawlDeps:
    """Everything the loop needs from outside.

    Arguments rather than imports, so core stays free of httpx and PostgreSQL
    and the whole loop can be driven by fakes.
    """

    fetcher: Fetcher
    frontier: MemoryFrontier | HostFrontier
    gate: UrlGate
    extractor: Extractor
    archive: BlobStore | NullBlobStore

    deduper: ContentDeduper | None = None
    """Content-level duplicate detection. Absent in Collect mode, where a
    handful of known pages cannot usefully collide."""

    soft_404: SoftNotFoundDetector | None = None
    """Pages that answer 200 and mean 404. A hit burns the URL pattern's
    budget rather than the page, because one soft 404 means the whole shape
    is generative (docs/05_SPIDER_ARCHITECTURE.md)."""


async def run_crawl(
    spec: CrawlSpec,
    deps: CrawlDeps,
    *,
    concurrency: int | None = None,
) -> AsyncGenerator[CrawlEvent, None]:
    """Crawl ``spec``, yielding events as they happen."""
    pump = EventPump()
    state = _Crawl(spec=spec, deps=deps, pump=pump)
    workers = concurrency or spec.limits.global_concurrency

    pump.spawn(state.drive(workers), name="crawl-driver")
    async for event in pump.stream():
        yield event


@dataclass(slots=True)
class _Crawl:
    spec: CrawlSpec
    deps: CrawlDeps
    pump: EventPump
    pages_fetched: int = 0
    records_extracted: int = 0

    # ------------------------------------------------------------- lifecycle

    async def drive(self, workers: int) -> None:
        await self.pump.emit(JobStarted(spec_id=str(self.spec.id), seeds=self.spec.seed_urls))
        started = time.perf_counter()

        await self._seed()

        pool = [asyncio.create_task(self._worker()) for _ in range(workers)]
        try:
            await asyncio.gather(*pool)
        finally:
            for task in pool:
                task.cancel()

        await self.pump.emit(
            JobCompleted(
                pages_fetched=self.pages_fetched,
                records_extracted=self.records_extracted,
                elapsed_s=round(time.perf_counter() - started, 3),
            )
        )
        await self.pump.finish()

    async def _seed(self) -> None:
        """Seeds go through the same enqueue gate as discovered links.

        A seed that is out of its own scope, or malformed, should fail the same
        way and for the same stated reason.
        """
        for raw in self.spec.seed_urls:
            try:
                url = self._normalize(raw)
            except UrlNormalizationError as exc:
                await self._reject(raw, RejectReason.MALFORMED, str(exc))
                continue
            await self._enqueue(url, depth=0, discovered_from=None)

    async def _worker(self) -> None:
        frontier = self.deps.frontier
        while True:
            if self.deps.gate.budget_exhausted:
                return
            item = await frontier.next()
            if item is None:
                if frontier.exhausted:
                    return
                # Queue is empty but somebody is still fetching, so more links
                # may be on the way. Yield rather than spin.
                await asyncio.sleep(_IDLE_SLEEP_S)
                continue
            try:
                await self._visit(item)
            finally:
                await frontier.done(item)

    # ----------------------------------------------------------------- page

    async def _visit(self, item: FrontierItem) -> None:
        verdict = await self.deps.gate.admit_fetch(item.url)
        if not verdict.admitted:
            assert verdict.reason is not None
            await self._reject(item.url.url, verdict.reason, verdict.detail)
            return

        outcome = await self.deps.fetcher.fetch(
            FetchRequest(
                url=item.url,
                depth=item.depth,
                mode=self.spec.fetch_mode,
                timeout_s=self.spec.limits.request_timeout_s,
                byte_limit=self.spec.limits.response_byte_limit,
                max_redirects=self.spec.limits.redirect_max,
            )
        )

        if isinstance(outcome, FetchFailure):
            self._back_off(item, outcome)
            await self.pump.emit(
                PageFailed(
                    url=item.url.url,
                    depth=item.depth,
                    error_kind=outcome.error_kind,
                    message=outcome.message,
                    retryable=outcome.retryable,
                )
            )
            return

        await self._on_response(item, outcome)

    async def _on_response(self, item: FrontierItem, response: FetchResponse) -> None:
        # Archive first and unconditionally: extractors change between phases,
        # the bytes do not.
        ref: ArchiveRef = self.deps.archive.put(response.body)

        self.pages_fetched += 1
        await self.pump.emit(
            PageFetched(
                url=item.url.url,
                status=response.status,
                content_type=response.content_type,
                bytes=len(response.body),
                elapsed_ms=response.elapsed_ms,
                depth=item.depth,
                fetch_mode=response.fetch_mode,
                redirects=response.redirects,
                final_url=response.final_url,
            )
        )

        if not self.deps.extractor.supports(response):
            await self._maybe_progress()
            return

        result = self.deps.extractor.extract(response)

        if result.canonical_url:
            await self._note_canonical(item, result.canonical_url)

        if await self._is_soft_404(item, result.text, len(result.records)):
            # The page is kept - it was fetched and archived - but its URL
            # shape is not explored further. One soft 404 means the pattern is
            # generative, so the rest of it is the same page.
            await self._maybe_progress()
            return

        if await self._is_duplicate(item, result.text):
            await self._maybe_progress()
            return

        if result.records:
            self.records_extracted += len(result.records)
            await self.pump.emit(
                RecordsExtracted(
                    url=item.url.url,
                    extractor=result.extractor,
                    count=len(result.records),
                    records=result.records,
                )
            )

        if result.links and self.spec.follow_links:
            await self._discover(item, result.links, base=response.final_url or item.url.url)

        del ref  # persisted by the sink; the loop only needed it stored
        await self._maybe_progress()

    def _back_off(self, item: FrontierItem, failure: FetchFailure) -> None:
        """Stand off from a host that refused.

        Not etiquette - a blocked host produces nothing, so waiting is the
        faster route to the data (docs/12_PERFORMANCE.md). The host's own
        ``Retry-After`` is honoured when it sent one, because a site saying
        how long to wait is the best information available.

        Only the host-partitioned frontier can do this; the FIFO has nowhere
        to put a per-host delay, and Collect mode does not need one.
        """
        frontier = self.deps.frontier
        if not isinstance(frontier, HostFrontier):
            return
        if failure.error_kind not in (ErrorKind.BLOCKED_429, ErrorKind.BLOCKED_403):
            return

        delay = failure.retry_after_s if failure.retry_after_s is not None else 30.0
        frontier.penalise(item.url.host, min(delay, 300.0))

    # --------------------------------------------------------- spider checks

    async def _is_soft_404(self, item: FrontierItem, text: str | None, records: int) -> bool:
        """A 200 that means 404.

        Burning the *pattern* rather than the URL is the point: a site that
        renders an empty template for ``/product/{n}`` will do it for every n,
        and stopping at one page would leave the other four hundred queued.
        """
        detector = self.deps.soft_404
        if detector is None or not detector.check(text, records_found=records):
            return False

        pattern = self.deps.gate.traps.pattern_of(item.url)
        self.deps.gate.traps.budget.exhaust(pattern)
        await self.pump.emit(
            PatternBudgetExhausted(pattern=pattern, limit=self.spec.spider.per_pattern_budget)
        )
        await self._reject(item.url.url, RejectReason.SOFT_404, "200 with no content")
        return True

    async def _is_duplicate(self, item: FrontierItem, text: str | None) -> bool:
        """The same content under a different address.

        URL dedupe never sees this: print views, mirrored paths and
        pagination that ran off the end are all distinct URLs serving one
        page (docs/05_SPIDER_ARCHITECTURE.md).
        """
        deduper = self.deps.deduper
        if deduper is None:
            return False

        verdict = deduper.check(item.url.url, text)
        if not verdict.is_duplicate:
            return False

        await self.pump.emit(
            DuplicateDetected(
                url=item.url.url,
                duplicate_of=verdict.of_url or "",
                via="content",
            )
        )
        return True

    # ------------------------------------------------------------ discovery

    async def _discover(self, item: FrontierItem, links: tuple[str, ...], base: str) -> None:
        enqueued = 0
        depth = item.depth + 1
        for href in links:
            try:
                url = self._normalize(href, base=base)
            except UrlNormalizationError:
                continue
            if await self._enqueue(url, depth=depth, discovered_from=item.url.url):
                enqueued += 1

        await self.pump.emit(LinksDiscovered(url=item.url.url, found=len(links), enqueued=enqueued))

    async def _enqueue(
        self, url: NormalizedUrl, *, depth: int, discovered_from: str | None
    ) -> bool:
        """Gate, then queue.

        Silent about duplicates: on a spider most links are ones already seen,
        and an event per rejection would drown the stream. Policy rejections
        are reported, because those are the ones worth tuning.
        """
        if self.deps.frontier.has_seen(url.dedupe_key):
            return False

        verdict = self.deps.gate.check_enqueue(url, depth)
        if not verdict.admitted:
            assert verdict.reason is not None
            await self._reject(url.url, verdict.reason, verdict.detail)
            if verdict.reason is RejectReason.PATTERN_BUDGET:
                await self._maybe_report_budget(url)
            return False

        return await self.deps.frontier.add(
            FrontierItem(
                url=url,
                depth=depth,
                discovered_from=discovered_from,
                # Scored on the way in, because that is where the information
                # is: the priority queue only reorders what it was given.
                priority=score_url(url.url, depth),
            )
        )

    async def _note_canonical(self, item: FrontierItem, canonical: str) -> None:
        """A declared canonical form is the cheapest duplicate signal there is,
        and it costs one fetch to learn - so spend it once and remember."""
        try:
            resolved = self._normalize(canonical, base=item.url.url)
        except UrlNormalizationError:
            return
        if resolved.dedupe_key == item.url.dedupe_key:
            return
        self.deps.frontier.mark_seen(resolved.dedupe_key)
        await self.pump.emit(
            DuplicateDetected(url=item.url.url, duplicate_of=resolved.url, via="canonical")
        )

    # ---------------------------------------------------------------- utils

    def _normalize(self, raw: str, *, base: str | None = None) -> NormalizedUrl:
        return normalize(raw, base=base, dedupe_whitelist=self.spec.spider.query_whitelist)

    async def _reject(self, url: str, reason: RejectReason, detail: str | None) -> None:
        await self.pump.emit(UrlRejected(url=url, reason=reason, detail=detail))

    async def _maybe_report_budget(self, url: NormalizedUrl) -> None:
        pattern = self.deps.gate.traps.pattern_of(url)
        if self.deps.gate.traps.budget.just_exhausted(pattern):
            await self.pump.emit(
                PatternBudgetExhausted(pattern=pattern, limit=self.spec.spider.per_pattern_budget)
            )

    async def _maybe_progress(self) -> None:
        if self.pages_fetched % PROGRESS_EVERY == 0:
            await self.pump.emit(
                Progress(
                    pages_done=self.pages_fetched,
                    pages_queued=self.deps.frontier.pending,
                    records_total=self.records_extracted,
                    hosts_active=getattr(self.deps.frontier, "hosts_active", 0),
                )
            )
