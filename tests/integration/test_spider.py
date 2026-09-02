"""The spider, end to end against the adversarial fixture.

The unit tests prove each piece works. These prove the pieces are *reached* -
that the sitemap is actually consulted before crawling, that a soft 404 takes
its pattern's budget rather than one page, and that a page seen twice under
different URLs is only extracted once.

docs/05_SPIDER_ARCHITECTURE.md
"""

from __future__ import annotations

import ipaddress
from collections import Counter
from collections.abc import Iterator

import pytest

from crwallm.schemas.events import (
    CrawlEvent,
    DuplicateDetected,
    JobCompleted,
    PageFetched,
    UrlRejected,
)
from crwallm.schemas.spec import CrawlLimits, CrawlSpec, SpiderConfig
from crwallm.schemas.types import CrawlMode, RejectReason
from crwallm.services.crawl import CrawlPlan
from crwallm.services.spider import SpiderSetup, open_spider
from tests.fixtures.malicious_server.server import MaliciousServer, RunningServer

pytestmark = pytest.mark.integration

LOOPBACK = [ipaddress.ip_network("127.0.0.0/8")]


@pytest.fixture(scope="module")
def server() -> Iterator[RunningServer]:
    s = MaliciousServer()
    try:
        yield s.start()
    finally:
        s.stop()


class LoopbackScope:
    """The fixture answers on 127.0.0.1, which has no registrable domain."""

    def contains(self, host: str) -> bool:
        return host.strip().lower() == "127.0.0.1"


def guard():  # type: ignore[no-untyped-def]
    from crwallm.policy.ssrf import SsrfGuard, StaticResolver

    return SsrfGuard(StaticResolver({}), allow_networks=LOOPBACK)  # type: ignore[arg-type]


def make_spec(server: RunningServer, path: str = "/", **kw: object) -> CrawlSpec:
    limits = kw.pop("limits", CrawlLimits(max_pages=40, max_depth=4, global_concurrency=4))
    return CrawlSpec(
        seed_urls=(server.url(path),),
        allowed_domains=("127.0.0.1",),
        mode=CrawlMode.SPIDER,
        follow_links=True,
        limits=limits,  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


async def run(
    server: RunningServer, spec: CrawlSpec, **kw: object
) -> tuple[list[CrawlEvent], SpiderSetup]:
    setup = SpiderSetup()
    events: list[CrawlEvent] = []
    async with open_spider(
        CrawlPlan(spec=spec),
        guard=guard(),
        scope=LoopbackScope(),  # type: ignore[arg-type]
        setup=setup,
        **kw,  # type: ignore[arg-type]
    ) as stream:
        async for event in stream:
            events.append(event)
    return events, setup


def fetched(events: list[CrawlEvent]) -> list[str]:
    return [e.url for e in events if isinstance(e, PageFetched)]


def rejects(events: list[CrawlEvent]) -> Counter[RejectReason]:
    return Counter(e.reason for e in events if isinstance(e, UrlRejected))


class TestSitemapSeeding:
    async def test_the_frontier_is_filled_before_crawling(self, server: RunningServer) -> None:
        """The whole reason sitemaps are read first: ten URLs for four
        requests, instead of ten fetches to discover ten pages."""
        _, setup = await run(server, make_spec(server))

        assert setup.seeding is not None
        assert setup.seeded_urls == 10
        assert setup.seeding.documents_fetched <= 6

    async def test_robots_txt_points_at_the_index(self, server: RunningServer) -> None:
        """The fixture's robots.txt disallows /shop and names a sitemap. Both
        are read; only one is obeyed (docs/17_NON_GOALS.md)."""
        _, setup = await run(server, make_spec(server))
        assert setup.seeding is not None
        assert any("sitemap_index" in s for s in setup.seeding.sources)

    async def test_nested_indexes_are_followed(self, server: RunningServer) -> None:
        _, setup = await run(server, make_spec(server))
        assert setup.seeding is not None
        assert len(setup.seeding.sources) >= 3, "index plus its shards"

    async def test_sitemap_urls_actually_get_crawled(self, server: RunningServer) -> None:
        events, _ = await run(server, make_spec(server))
        urls = fetched(events)
        assert sum(1 for u in urls if "/shop/item/" in u) >= 8

    async def test_seeding_can_be_switched_off(self, server: RunningServer) -> None:
        _, setup = await run(server, make_spec(server), use_sitemaps=False)
        assert setup.seeding is None
        assert setup.seeded_urls == 0

    async def test_a_site_without_a_sitemap_still_crawls(self, server: RunningServer) -> None:
        """A missing sitemap is normal, not an error - the crawl falls back to
        following links."""
        events, _ = await run(server, make_spec(server, "/a"))
        assert isinstance(events[-1], JobCompleted)
        assert fetched(events)


class TestSoftNotFound:
    async def test_a_ghost_pattern_stops_costing_budget(self, server: RunningServer) -> None:
        """``/ghost/{n}`` is an infinite family of 200s that all say "not
        found". Without detection the crawl would collect every one of them;
        with it, one hit burns the pattern."""
        spec = make_spec(
            server,
            "/ghost/1",
            limits=CrawlLimits(max_pages=30, max_depth=3, global_concurrency=2),
            spider=SpiderConfig(per_pattern_budget=100),
        )
        events, _ = await run(server, spec, use_sitemaps=False)

        assert RejectReason.SOFT_404 in rejects(events)
        assert len(fetched(events)) <= 2

    async def test_a_real_page_is_not_mistaken_for_one(self, server: RunningServer) -> None:
        """The regression that mattered: a page whose whole content was "b"
        was flagged, and that took its entire URL pattern with it."""
        events, _ = await run(server, make_spec(server), use_sitemaps=False)
        soft = [
            e for e in events if isinstance(e, UrlRejected) and e.reason is RejectReason.SOFT_404
        ]
        assert soft == [], [e.url for e in soft]


class TestContentDuplicates:
    async def test_the_same_article_under_two_urls_is_caught_once(
        self, server: RunningServer
    ) -> None:
        """``/mirror/{n}`` serves one article at many addresses, differing only
        in a footer - which an exact hash misses entirely.

        Both are seeded directly because nothing links between them: the piece
        under test is the deduper, not link discovery.
        """
        spec = CrawlSpec(
            seed_urls=(server.url("/mirror/1"), server.url("/mirror/2")),
            allowed_domains=("127.0.0.1",),
            mode=CrawlMode.SPIDER,
            follow_links=True,
            limits=CrawlLimits(max_pages=10, max_depth=1, global_concurrency=1),
        )
        events, _ = await run(server, spec, use_sitemaps=False)

        assert len(fetched(events)) == 2, "both were fetched"
        content_dupes = [
            e for e in events if isinstance(e, DuplicateDetected) and e.via == "content"
        ]
        assert len(content_dupes) == 1, "the second is recognised as the first"

    async def test_genuinely_different_pages_survive(self, server: RunningServer) -> None:
        """The failure that would matter more: a deduper that collapses a
        catalogue into one row."""
        spec = CrawlSpec(
            seed_urls=tuple(server.url(f"/shop/item/{i}") for i in range(1, 6)),
            allowed_domains=("127.0.0.1",),
            mode=CrawlMode.SPIDER,
            # Spider mode requires following, so depth 0 is what keeps the
            # crawl to exactly the seeded pages.
            follow_links=True,
            limits=CrawlLimits(max_pages=10, max_depth=0, global_concurrency=1),
        )
        events, _ = await run(server, spec, use_sitemaps=False)

        content_dupes = [
            e for e in events if isinstance(e, DuplicateDetected) and e.via == "content"
        ]
        assert content_dupes == [], [e.url for e in content_dupes]

    async def test_canonical_and_content_duplicates_are_distinguished(
        self, server: RunningServer
    ) -> None:
        """Two different findings: one is the site telling us, the other is us
        noticing. Reporting them together hides which mechanism fired."""
        events, _ = await run(server, make_spec(server), use_sitemaps=False)
        kinds = {e.via for e in events if isinstance(e, DuplicateDetected)}
        assert kinds <= {"canonical", "content", "url"}
        assert "canonical" in kinds


class TestHostScheduling:
    async def test_a_crawl_completes_and_reports_hosts(self, server: RunningServer) -> None:
        from crwallm.schemas.events import Progress

        events, _ = await run(server, make_spec(server))
        assert isinstance(events[-1], JobCompleted)
        progress = [e for e in events if isinstance(e, Progress)]
        if progress:
            assert progress[-1].hosts_active >= 0

    async def test_pages_are_never_fetched_twice(self, server: RunningServer) -> None:
        events, _ = await run(server, make_spec(server))
        urls = fetched(events)
        assert len(urls) == len(set(urls))

    async def test_the_page_budget_still_stops_the_crawl(self, server: RunningServer) -> None:
        spec = make_spec(
            server,
            "/calendar/2031/07",
            limits=CrawlLimits(max_pages=6, max_depth=20, global_concurrency=2),
            spider=SpiderConfig(per_pattern_budget=1000),
        )
        events, _ = await run(server, spec, use_sitemaps=False)
        assert len(fetched(events)) <= 6

    async def test_the_pattern_budget_stops_it_sooner(self, server: RunningServer) -> None:
        spec = make_spec(
            server,
            "/calendar/2031/07",
            limits=CrawlLimits(max_pages=200, max_depth=20, global_concurrency=2),
            spider=SpiderConfig(per_pattern_budget=4),
        )
        events, _ = await run(server, spec, use_sitemaps=False)
        assert len(fetched(events)) <= 6
