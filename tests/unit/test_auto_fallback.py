"""Escalating to the browser, without starting one.

The decision is what these test, and the decision is arithmetic: did
extraction produce a record? A real browser would make them slow and would not
exercise anything the logic depends on - the fetchers here are stubs that
return exactly what the branch under test needs.

The rule being defended is that a browser costs twenty to fifty times an HTTP
fetch, so it must run rarely and must never run twice for one page
(docs/04_CRAWLING_ARCHITECTURE.md).
"""

from __future__ import annotations

from crwallm.crawler.contracts import ExtractionResult, FetchFailure, FetchRequest, FetchResponse
from crwallm.crawler.frontier.memory import MemoryFrontier
from crwallm.crawler.traversal import CrawlDeps, run_crawl
from crwallm.policy.gate import UrlGate
from crwallm.policy.ssrf import SsrfGuard, StaticResolver
from crwallm.schemas.events import CrawlEvent, PageFetched, RecordsExtracted
from crwallm.schemas.spec import CrawlLimits, CrawlSpec
from crwallm.schemas.types import ErrorKind, FetchMode
from crwallm.storage.blob import NullBlobStore

SEED = "https://shop.test/list"

HTTP_HTML = b"<html><body><div id='app'></div></body></html>"
RENDERED_HTML = b"<html><body><div class='row'>Keyboard</div></body></html>"


class StubFetcher:
    """Returns a fixed body, and counts how often it was asked."""

    def __init__(self, body: bytes, mode: FetchMode, *, fail: bool = False) -> None:
        self.body = body
        self.mode = mode
        self.fail = fail
        self.calls = 0
        self.closed = False

    async def fetch(self, request: FetchRequest) -> FetchResponse | FetchFailure:
        self.calls += 1
        if self.fail:
            return FetchFailure(
                url=request.url,
                error_kind=ErrorKind.INTERNAL,
                message="no browser here",
            )
        return FetchResponse(
            url=request.url,
            status=200,
            headers={"content-type": "text/html"},
            body=self.body,
            elapsed_ms=1,
            fetch_mode=self.mode,
        )

    async def aclose(self) -> None:
        self.closed = True


class StubExtractor:
    """Records only when the body carries the marker class."""

    name = "stub"

    def supports(self, response: FetchResponse) -> bool:
        return True

    def extract(self, response: FetchResponse) -> ExtractionResult:
        if b"class='row'" in response.body:
            return ExtractionResult(extractor=self.name, records=({"title": "Keyboard"},))
        return ExtractionResult(extractor=self.name, records=())


class AllowAll:
    def contains(self, host: str) -> bool:
        return True


def spec(mode: FetchMode) -> CrawlSpec:
    return CrawlSpec(
        seed_urls=(SEED,),
        allowed_domains=("shop.test",),
        fetch_mode=mode,
        limits=CrawlLimits(max_pages=5, max_depth=0, global_concurrency=1),
    )


async def crawl(
    mode: FetchMode,
    http: StubFetcher,
    browser: StubFetcher | None,
) -> list[CrawlEvent]:
    guard = SsrfGuard(StaticResolver({"shop.test": ["93.184.216.34"]}))  # type: ignore[arg-type]
    crawl_spec = spec(mode)
    deps = CrawlDeps(
        fetcher=http,
        browser=browser,
        frontier=MemoryFrontier(),
        gate=UrlGate.build(crawl_spec, guard, scope=AllowAll()),  # type: ignore[arg-type]
        extractor=StubExtractor(),
        archive=NullBlobStore(),
    )
    return [event async for event in run_crawl(crawl_spec, deps)]


def fetched(events: list[CrawlEvent]) -> list[PageFetched]:
    return [e for e in events if isinstance(e, PageFetched)]


def records(events: list[CrawlEvent]) -> int:
    return sum(e.count for e in events if isinstance(e, RecordsExtracted))


class TestWhenItEscalates:
    async def test_zero_records_over_http_reaches_for_the_browser(self) -> None:
        """The whole feature: a page whose content is written by a script."""
        http = StubFetcher(HTTP_HTML, FetchMode.HTTP)
        browser = StubFetcher(RENDERED_HTML, FetchMode.BROWSER)
        events = await crawl(FetchMode.AUTO, http, browser)

        assert browser.calls == 1
        assert records(events) == 1

    async def test_records_over_http_never_opens_the_browser(self) -> None:
        """Measured on a server-rendered page: auto finished in 0.74s where
        the browser path took 2.4s. That difference is this branch."""
        http = StubFetcher(RENDERED_HTML, FetchMode.HTTP)
        browser = StubFetcher(RENDERED_HTML, FetchMode.BROWSER)
        events = await crawl(FetchMode.AUTO, http, browser)

        assert browser.calls == 0
        assert records(events) == 1

    async def test_http_mode_never_escalates_even_with_a_browser_present(self) -> None:
        """``--mode http`` is an instruction, not a preference."""
        http = StubFetcher(HTTP_HTML, FetchMode.HTTP)
        browser = StubFetcher(RENDERED_HTML, FetchMode.BROWSER)
        await crawl(FetchMode.HTTP, http, browser)

        assert browser.calls == 0

    async def test_auto_without_a_browser_just_reports_nothing(self) -> None:
        """A machine with no Chromium still runs the crawl."""
        http = StubFetcher(HTTP_HTML, FetchMode.HTTP)
        events = await crawl(FetchMode.AUTO, http, None)

        assert records(events) == 0
        assert len(fetched(events)) == 1


class TestItOnlyCountsOnce:
    """The reason the decision happens before the response is committed."""

    async def test_an_escalated_page_is_fetched_once_in_the_log(self) -> None:
        """Two ``PageFetched`` events for one URL would make every counter -
        pages crawled, throughput, the page budget - quietly wrong."""
        http = StubFetcher(HTTP_HTML, FetchMode.HTTP)
        browser = StubFetcher(RENDERED_HTML, FetchMode.BROWSER)
        events = await crawl(FetchMode.AUTO, http, browser)

        assert len(fetched(events)) == 1

    async def test_the_page_is_reported_as_rendered(self) -> None:
        """Which fetcher won is the operator's answer to "why was this slow"."""
        http = StubFetcher(HTTP_HTML, FetchMode.HTTP)
        browser = StubFetcher(RENDERED_HTML, FetchMode.BROWSER)
        events = await crawl(FetchMode.AUTO, http, browser)

        assert fetched(events)[0].fetch_mode is FetchMode.BROWSER

    async def test_a_page_that_did_not_escalate_reports_http(self) -> None:
        http = StubFetcher(RENDERED_HTML, FetchMode.HTTP)
        browser = StubFetcher(RENDERED_HTML, FetchMode.BROWSER)
        events = await crawl(FetchMode.AUTO, http, browser)

        assert fetched(events)[0].fetch_mode is FetchMode.HTTP

    async def test_the_page_budget_counts_it_once(self) -> None:
        http = StubFetcher(HTTP_HTML, FetchMode.HTTP)
        browser = StubFetcher(RENDERED_HTML, FetchMode.BROWSER)
        await crawl(FetchMode.AUTO, http, browser)

        assert http.calls == 1
        assert browser.calls == 1


class TestWhenTheBrowserFails:
    async def test_the_http_response_stands(self) -> None:
        """A browser that could not render is not evidence that the page is
        empty. Dropping the page would lose one that was fetched fine."""
        http = StubFetcher(HTTP_HTML, FetchMode.HTTP)
        browser = StubFetcher(RENDERED_HTML, FetchMode.BROWSER, fail=True)
        events = await crawl(FetchMode.AUTO, http, browser)

        page = fetched(events)
        assert len(page) == 1
        assert page[0].fetch_mode is FetchMode.HTTP

    async def test_a_failed_render_is_not_a_failed_page(self) -> None:
        from crwallm.schemas.events import PageFailed

        http = StubFetcher(HTTP_HTML, FetchMode.HTTP)
        browser = StubFetcher(RENDERED_HTML, FetchMode.BROWSER, fail=True)
        events = await crawl(FetchMode.AUTO, http, browser)

        assert [e for e in events if isinstance(e, PageFailed)] == []


class TestRenderedButStillEmpty:
    async def test_the_rendered_body_is_the_one_kept(self) -> None:
        """ "Rendered and still empty" is a different diagnosis from "never
        rendered", and only the rendered body tells the operator which they
        have - so it is what gets archived and shown."""
        http = StubFetcher(HTTP_HTML, FetchMode.HTTP)
        browser = StubFetcher(b"<html><body>still nothing</body></html>", FetchMode.BROWSER)
        events = await crawl(FetchMode.AUTO, http, browser)

        page = fetched(events)[0]
        assert page.fetch_mode is FetchMode.BROWSER
        assert records(events) == 0
