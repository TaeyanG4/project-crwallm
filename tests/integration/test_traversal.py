"""The crawl loop, end to end against the adversarial fixture.

The unit tests prove each guard works. These prove the loop *reaches* them in
the right order and stops when it should - which is where a crawler actually
fails: not by admitting a bad URL, but by never asking about it, or by never
stopping.

docs/04_CRAWLING_ARCHITECTURE.md, docs/05_SPIDER_ARCHITECTURE.md
"""

from __future__ import annotations

import ipaddress
from collections import Counter
from collections.abc import AsyncIterator, Iterator

import pytest

from crwallm.crawler.adapters import collect
from crwallm.crawler.extraction.css import CssExtractor, CssSpec, FieldSpec
from crwallm.crawler.fetching.http import SafeHttpFetcher
from crwallm.crawler.frontier.memory import MemoryFrontier
from crwallm.crawler.traversal import CrawlDeps, run_crawl
from crwallm.policy.gate import UrlGate
from crwallm.policy.ssrf import SsrfGuard, StaticResolver
from crwallm.schemas.events import (
    CrawlEvent,
    JobCompleted,
    JobStarted,
    PageFailed,
    PageFetched,
    RecordsExtracted,
    UrlRejected,
)
from crwallm.schemas.spec import CrawlLimits, CrawlSpec, SpiderConfig, UrlFilters
from crwallm.schemas.types import CrawlMode, ErrorKind, RejectReason
from crwallm.storage.blob import BlobStore, NullBlobStore
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


@pytest.fixture
async def fetcher() -> AsyncIterator[SafeHttpFetcher]:
    guard = SsrfGuard(
        StaticResolver({}),  # type: ignore[arg-type]
        allow_networks=LOOPBACK,
    )
    f = SafeHttpFetcher(guard, http2=False)
    try:
        yield f
    finally:
        await f.aclose()


def make_spec(server: RunningServer, **overrides: object) -> CrawlSpec:
    defaults: dict[str, object] = {
        "seed_urls": (server.url("/"),),
        "allowed_domains": ("example.com",),
        "mode": CrawlMode.COLLECT,
        "limits": CrawlLimits(max_pages=20, max_depth=2, global_concurrency=4),
    }
    defaults.update(overrides)
    return CrawlSpec(**defaults)  # type: ignore[arg-type]


class _LoopbackScope:
    """The fixture answers on 127.0.0.1, which has no registrable domain.

    Rather than weakening ``DomainScope`` for tests, the gate is built with a
    scope object that admits the fixture host and nothing else - so the scope
    check still runs, and still rejects everything off-site.
    """

    def __init__(self, host: str) -> None:
        self._host = host.lower()

    def contains(self, host: str) -> bool:
        return host.strip().lower().rstrip(".") == self._host


def build_deps(
    server: RunningServer,
    spec: CrawlSpec,
    fetcher: SafeHttpFetcher,
    *,
    extractor: CssExtractor | None = None,
    archive: BlobStore | NullBlobStore | None = None,
) -> CrawlDeps:
    guard = SsrfGuard(
        StaticResolver({}),  # type: ignore[arg-type]
        allow_networks=LOOPBACK,
    )
    gate = UrlGate.build(spec, guard, scope=_LoopbackScope("127.0.0.1"))
    return CrawlDeps(
        fetcher=fetcher,
        frontier=MemoryFrontier(),
        gate=gate,
        extractor=extractor or CssExtractor(CssSpec()),
        archive=archive or NullBlobStore(),
    )


def kinds(events: list[CrawlEvent]) -> Counter[str]:
    return Counter(e.type for e in events)


class TestBasicCrawl:
    async def test_a_single_page_crawl_completes(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        spec = make_spec(server)
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        assert isinstance(outcome.events[0], JobStarted)
        assert isinstance(outcome.events[-1], JobCompleted)
        assert outcome.pages_fetched == 1

    async def test_following_links_reaches_depth(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        """The fixture index links to /a and /b; /a links to /b."""
        spec = make_spec(
            server,
            follow_links=True,
            mode=CrawlMode.SPIDER,
            limits=CrawlLimits(max_pages=10, max_depth=2, global_concurrency=2),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        fetched = {e.url for e in outcome.events if isinstance(e, PageFetched)}
        assert any(u.endswith("/a") for u in fetched)
        assert any(u.endswith("/b") for u in fetched)

    async def test_a_page_is_never_fetched_twice(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        """/a and the index both link to /b."""
        spec = make_spec(
            server,
            follow_links=True,
            mode=CrawlMode.SPIDER,
            limits=CrawlLimits(max_pages=20, max_depth=3, global_concurrency=4),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        urls = [e.url for e in outcome.events if isinstance(e, PageFetched)]
        assert len(urls) == len(set(urls))


class TestBudgets:
    async def test_max_pages_stops_the_crawl(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        """Against an infinite calendar, the page budget is the last line."""
        spec = make_spec(
            server,
            seed_urls=(server.url("/calendar/2031/07"),),
            follow_links=True,
            mode=CrawlMode.SPIDER,
            limits=CrawlLimits(max_pages=5, max_depth=32, global_concurrency=2),
            spider=SpiderConfig(per_pattern_budget=1000),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        fetched = sum(1 for e in outcome.events if isinstance(e, PageFetched))
        assert fetched <= 5

    async def test_pattern_budget_stops_it_sooner(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        """The point of per-pattern budgets: the calendar dies at its own
        limit, long before the page budget is touched."""
        spec = make_spec(
            server,
            seed_urls=(server.url("/calendar/2031/07"),),
            follow_links=True,
            mode=CrawlMode.SPIDER,
            limits=CrawlLimits(max_pages=200, max_depth=32, global_concurrency=2),
            spider=SpiderConfig(per_pattern_budget=4),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        fetched = sum(1 for e in outcome.events if isinstance(e, PageFetched))
        assert fetched <= 5, "the calendar consumed more than its budget"
        assert "pattern.budget_exhausted" in kinds(outcome.events)

    async def test_max_depth_is_honoured(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        spec = make_spec(
            server,
            follow_links=True,
            mode=CrawlMode.SPIDER,
            limits=CrawlLimits(max_pages=50, max_depth=1, global_concurrency=2),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        depths = {e.depth for e in outcome.events if isinstance(e, PageFetched)}
        assert max(depths) <= 1

    async def test_session_trap_does_not_run_away(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        """Every link carries a fresh session id, so URL dedupe never fires.

        Only the pattern budget stops this, and only because long identifier
        segments collapse to one placeholder.
        """
        spec = make_spec(
            server,
            seed_urls=(server.url("/session/0000000000000000/page"),),
            follow_links=True,
            mode=CrawlMode.SPIDER,
            limits=CrawlLimits(max_pages=100, max_depth=32, global_concurrency=2),
            spider=SpiderConfig(per_pattern_budget=3),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        fetched = sum(1 for e in outcome.events if isinstance(e, PageFetched))
        assert fetched <= 4


class TestPolicyReachesTheLoop:
    async def test_offsite_links_are_rejected_for_scope(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        spec = make_spec(
            server,
            seed_urls=(server.url("/"), "https://example.com/"),
            allowed_domains=("example.com",),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        rejected = [e for e in outcome.events if isinstance(e, UrlRejected)]
        assert any(e.reason is RejectReason.SCOPE for e in rejected)

    async def test_redirect_to_metadata_surfaces_as_a_page_failure(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        """The guard runs inside the fetcher's redirect loop, so the crawl sees
        a classified failure rather than a fetched page."""
        spec = make_spec(server, seed_urls=(server.url("/redirect/metadata"),))
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        failures = [e for e in outcome.events if isinstance(e, PageFailed)]
        assert any(e.error_kind is ErrorKind.SSRF_REJECT for e in failures)

    async def test_url_filters_apply_to_discovered_links(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        spec = make_spec(
            server,
            follow_links=True,
            mode=CrawlMode.SPIDER,
            url_filters=UrlFilters(exclude=(r"/b$",)),
            limits=CrawlLimits(max_pages=20, max_depth=2, global_concurrency=2),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        fetched = {e.url for e in outcome.events if isinstance(e, PageFetched)}
        assert not any(u.endswith("/b") for u in fetched)

    async def test_oversized_response_is_a_failure_not_a_crash(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        spec = make_spec(
            server,
            seed_urls=(server.url("/huge"),),
            limits=CrawlLimits(max_pages=5, response_byte_limit=50_000),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        failures = [e for e in outcome.events if isinstance(e, PageFailed)]
        assert any(e.error_kind is ErrorKind.SIZE_EXCEEDED for e in failures)
        assert isinstance(outcome.events[-1], JobCompleted)


class TestExtraction:
    async def test_records_flow_through_the_loop(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        spec = make_spec(server, seed_urls=(server.url("/calendar/2031/07"),))
        extractor = CssExtractor(
            CssSpec(fields=(FieldSpec("heading", "h1", "text"),), follow_links=False)
        )
        outcome = await collect(
            run_crawl(spec, build_deps(server, spec, fetcher, extractor=extractor))
        )

        records = [e for e in outcome.events if isinstance(e, RecordsExtracted)]
        assert records
        assert outcome.records[0]["heading"] == "2031-07"

    async def test_canonical_link_marks_the_alternate_as_seen(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        """The index declares /canonical-target as canonical, so that URL must
        not also be fetched on its own."""
        spec = make_spec(
            server,
            follow_links=True,
            mode=CrawlMode.SPIDER,
            limits=CrawlLimits(max_pages=20, max_depth=2, global_concurrency=1),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))
        assert "duplicate.detected" in kinds(outcome.events)


class TestArchiving:
    async def test_bodies_are_archived_and_readable(
        self, server: RunningServer, fetcher: SafeHttpFetcher, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """The point of the archive: re-extraction without re-fetching."""
        store = BlobStore(tmp_path / "archive")
        spec = make_spec(server, seed_urls=(server.url("/"),))
        await collect(run_crawl(spec, build_deps(server, spec, fetcher, archive=store)))

        count, size = store.stats()
        assert count == 1
        assert size > 0

        digest = BlobStore.digest_of(b"")  # a body we did not store
        assert store.get(digest) is None

    async def test_identical_bodies_share_one_blob(
        self, server: RunningServer, fetcher: SafeHttpFetcher, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """/duplicate/1 and /duplicate/2 serve identical bytes."""
        store = BlobStore(tmp_path / "archive")
        spec = make_spec(
            server,
            seed_urls=(server.url("/duplicate/1"), server.url("/duplicate/2")),
            limits=CrawlLimits(max_pages=5, global_concurrency=1),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher, archive=store)))

        assert outcome.pages_fetched == 2
        count, _ = store.stats()
        assert count == 1, "content addressing should collapse identical bodies"


class TestTermination:
    async def test_the_stream_always_ends_with_a_terminal_event(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        """Whatever happens, a consumer must be able to tell the crawl is over."""
        for seed in ("/", "/huge", "/redirect/loop", "/status/500"):
            spec = make_spec(
                server,
                seed_urls=(server.url(seed),),
                limits=CrawlLimits(max_pages=3, response_byte_limit=50_000),
            )
            outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))
            assert isinstance(outcome.events[-1], JobCompleted), seed

    async def test_concurrency_does_not_duplicate_or_hang(
        self, server: RunningServer, fetcher: SafeHttpFetcher
    ) -> None:
        """Several workers share one frontier; the queue emptying while a peer
        still holds a page must not end the crawl early."""
        spec = make_spec(
            server,
            follow_links=True,
            mode=CrawlMode.SPIDER,
            limits=CrawlLimits(max_pages=30, max_depth=3, global_concurrency=8),
        )
        outcome = await collect(run_crawl(spec, build_deps(server, spec, fetcher)))

        urls = [e.url for e in outcome.events if isinstance(e, PageFetched)]
        assert len(urls) == len(set(urls))
        assert isinstance(outcome.events[-1], JobCompleted)
