"""Assembling a spider run.

Collect and Spider need different machinery, and this is where that choice is
made once rather than at every call site.

A Collect run visits a handful of known pages: a FIFO is right, and content
deduplication would be answering a question nobody asked. A Spider run walks a
site it has not seen: it needs sitemap seeding, per-host scheduling, priority
ordering, and detection for the two things that waste a budget silently -
duplicate content and pages that answer 200 while meaning 404
(docs/05_SPIDER_ARCHITECTURE.md).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from crwallm.crawler.contracts import FrontierItem
from crwallm.crawler.dedupe import ContentDeduper, SoftNotFoundDetector
from crwallm.crawler.discovery.seeding import SeedingReport, seed_from_sitemaps
from crwallm.crawler.frontier.scheduler import HostFrontier, score_url
from crwallm.crawler.traversal import CrawlDeps, run_crawl
from crwallm.policy.gate import Scope, UrlGate
from crwallm.policy.local import build_guard
from crwallm.policy.ssrf import SsrfGuard
from crwallm.schemas.events import CrawlEvent
from crwallm.schemas.types import FetchMode
from crwallm.services.crawl import CrawlPlan, build_extractor
from crwallm.storage.blob import BlobStore, NullBlobStore

__all__ = ["SpiderSetup", "open_spider"]


@dataclass(slots=True)
class SpiderSetup:
    """What happened before the first page was fetched.

    Surfaced rather than logged: "8,000 URLs from 6 sitemap documents" and
    "no sitemap, falling back to links" describe two very different crawls,
    and the operator should be able to tell which one just started.
    """

    seeding: SeedingReport | None = None
    seeded_urls: int = 0

    def summary(self) -> str:
        if self.seeding is None:
            return "seeded from the given URLs"
        return f"{self.seeding.summary()}; {self.seeded_urls} queued"


@contextlib.asynccontextmanager
async def open_spider(
    plan: CrawlPlan,
    *,
    archive_dir: Path | None = None,
    scope: Scope | None = None,
    guard: SsrfGuard | None = None,
    allow_local: bool = False,
    use_sitemaps: bool = True,
    dedupe_content: bool = True,
    setup: SpiderSetup | None = None,
) -> AsyncIterator[AsyncIterator[CrawlEvent]]:
    """Build a spider, seed it, and yield its event stream.

    A context manager because the fetcher owns a connection pool: leaving it
    open leaks sockets, closing it early truncates the crawl.
    """
    from crwallm.crawler.fetching.http import SafeHttpFetcher
    from crwallm.services.crawl import browser_for

    spec = plan.spec
    resolved_guard = guard if guard is not None else build_guard(allow_local=allow_local)

    # Sitemaps are fetched over HTTP whatever the page mode is: an XML
    # document has nothing to render, and opening a browser for one would be
    # the browser's cost with none of its benefit.
    browser = (
        browser_for(spec, resolved_guard)
        if plan.extracts_records or spec.fetch_mode is FetchMode.BROWSER
        else None
    )
    fetcher = SafeHttpFetcher(resolved_guard)
    page_fetcher = browser if spec.fetch_mode is FetchMode.BROWSER and browser else fetcher

    frontier = HostFrontier(
        per_host_concurrency=spec.limits.per_host_concurrency,
        min_interval_s=spec.limits.min_interval_ms / 1000,
    )

    deps = CrawlDeps(
        fetcher=page_fetcher,
        browser=browser if spec.fetch_mode is FetchMode.AUTO else None,
        frontier=frontier,
        gate=UrlGate.build(spec, resolved_guard, scope=scope),
        extractor=build_extractor(plan),
        sieve=plan.sieve,
        archive=BlobStore(archive_dir) if archive_dir else NullBlobStore(),
        deduper=ContentDeduper() if dedupe_content else None,
        soft_404=SoftNotFoundDetector(),
    )

    report = setup if setup is not None else SpiderSetup()

    try:
        if use_sitemaps:
            await _seed_from_sitemaps(deps, plan, report)
        yield run_crawl(spec, deps)
    finally:
        await fetcher.aclose()
        if browser is not None and browser is not page_fetcher:
            await browser.aclose()


async def _seed_from_sitemaps(deps: CrawlDeps, plan: CrawlPlan, report: SpiderSetup) -> None:
    """Pre-fill the frontier from the site's own index.

    Every URL that lands here is one the crawl does not have to *discover*,
    and discovery costs a fetch and a parse per hop. The entries also carry
    the site's own priority hints, which are better than anything inferable
    from a path.

    Seeded URLs still pass the enqueue gate: a sitemap listing something out
    of scope, or a trap shape, gets the same treatment as a link would.
    """
    spec = plan.spec
    seeded = 0

    for seed in spec.seed_urls:
        result = await seed_from_sitemaps(
            deps.fetcher, seed, dedupe_whitelist=spec.spider.query_whitelist
        )
        report.seeding = result

        for entry in result.entries:
            verdict = deps.gate.check_enqueue(entry.url, 0)
            if not verdict.admitted:
                continue
            added = await deps.frontier.add(
                FrontierItem(
                    url=entry.url,
                    depth=0,
                    discovered_from="sitemap",
                    priority=score_url(entry.url.url, 0, from_sitemap=True, hint=entry.priority),
                )
            )
            if added:
                seeded += 1

        # One sitemap walk is enough. Several seeds on one site would find the
        # same index and pay for it again.
        break

    report.seeded_urls = seeded
