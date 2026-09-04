"""The browser, against the adversarial fixture and a real Chromium.

These are the tests a stub cannot stand in for. Whether a route handler
actually stops a request, whether ``file://`` is reachable from a page, and
whether the page pool survives a crawl are all facts about Chromium, and the
security ones are the reason this file is worth its runtime.

The HTTP fetcher resolves once and connects to the address it checked. A
browser does its own DNS, so the guard is applied at two other points instead:
before navigation, and on every request the page makes
(docs/11_SECURITY_MODEL.md).
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from crwallm.crawler.contracts import FetchFailure, FetchRequest, FetchResponse
from crwallm.crawler.fetching.browser import (
    BLOCKED_RESOURCE_TYPES,
    BrowserFetcher,
    ScrollPolicy,
)
from crwallm.policy.ssrf import SsrfGuard, SystemResolver
from crwallm.policy.url import normalize
from crwallm.schemas.types import ErrorKind, FetchMode
from tests.fixtures.malicious_server.server import MaliciousServer, RunningServer

LOOPBACK = [ipaddress.ip_network("127.0.0.0/8")]


def chromium_available() -> bool:
    """Whether a browser can actually be launched here.

    The import is not the question. ``playwright`` is a declared dependency, so
    it is always importable, while the browser binary is a separate download
    (``python -m playwright install chromium``) that a fresh checkout does not
    have. Checking the import meant these never skipped and always failed -
    with "Executable doesn't exist", after a reason that said the package was
    missing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    try:
        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not chromium_available(),
        reason="Chromium is not installed - run: python -m playwright install chromium",
    ),
]


@pytest.fixture(scope="module")
def server() -> Iterator[RunningServer]:
    s = MaliciousServer()
    try:
        yield s.start()
    finally:
        s.stop()


def loopback_guard() -> SsrfGuard:
    """Allowed to reach the fixture on 127.0.0.1 and nothing else internal."""
    return SsrfGuard(SystemResolver(), allow_networks=LOOPBACK)


@pytest.fixture
async def fetcher() -> AsyncIterator[BrowserFetcher]:
    f = BrowserFetcher(loopback_guard(), max_pages=2)
    try:
        yield f
    finally:
        await f.aclose()


def _items(result: FetchResponse) -> int:
    """Count list items in the rendered DOM.

    Not a substring search over the body: `item-` also appears in the script
    that creates them, so counting bytes counts the source too.
    """
    from selectolax.lexbor import LexborHTMLParser

    return len(LexborHTMLParser(result.body.decode()).css("li.item"))


def request_for(url: str, *, timeout_s: float = 30.0, byte_limit: int = 4_000_000) -> FetchRequest:
    return FetchRequest(
        url=normalize(url),
        depth=0,
        mode=FetchMode.BROWSER,
        timeout_s=timeout_s,
        byte_limit=byte_limit,
    )


class TestRendering:
    async def test_a_page_written_by_script_is_returned(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        """The entire justification for the browser: content that does not
        exist in the bytes the server sent.

        Asserted against the parsed DOM. A substring search over the body
        passes on the *script* that would write the element, so the original
        version of this test was green while the page rendered 2 times in 5.
        """
        from selectolax.lexbor import LexborHTMLParser

        result = await fetcher.fetch(request_for(server.url("/js/rendered")))
        assert isinstance(result, FetchResponse)
        rendered = LexborHTMLParser(result.body.decode()).css("div.written-by-script")
        assert len(rendered) == 5

    async def test_xhr_content_renders_every_time(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        """The reason settling has a floor rather than a default of zero.

        Measured at zero: 2 renders in 5. A browser that gets the content half
        the time is worse than no browser, because ``auto`` treats an empty
        render as "this page really is empty".
        """
        from selectolax.lexbor import LexborHTMLParser

        for _ in range(4):
            result = await fetcher.fetch(request_for(server.url("/js/rendered")))
            assert isinstance(result, FetchResponse)
            assert LexborHTMLParser(result.body.decode()).css("div.written-by-script")

    async def test_the_response_reports_browser_mode(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        """Which fetcher produced a page is the operator's answer to "why was
        this slow", so it has to survive into the event log."""
        result = await fetcher.fetch(request_for(server.url("/js/rendered")))
        assert isinstance(result, FetchResponse)
        assert result.fetch_mode is FetchMode.BROWSER

    async def test_the_body_is_always_utf8_html(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        """``page.content()`` is serialised from the parsed DOM, so whatever
        the source declared, what comes back here is UTF-8 - and saying
        otherwise would send the decoder looking for a charset that is gone."""
        result = await fetcher.fetch(request_for(server.url("/js/rendered")))
        assert isinstance(result, FetchResponse)
        assert "utf-8" in result.headers["content-type"]
        result.body.decode("utf-8")

    async def test_an_oversized_render_is_refused(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        result = await fetcher.fetch(request_for(server.url("/js/rendered"), byte_limit=64))
        assert isinstance(result, FetchFailure)
        assert result.error_kind is ErrorKind.SIZE_EXCEEDED

    async def test_a_timeout_is_reported_as_one(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        """Not as a broken site: the remedy is a longer timeout, and the two
        are indistinguishable if both arrive as INTERNAL."""
        result = await fetcher.fetch(request_for(server.url("/slow"), timeout_s=0.5))
        assert isinstance(result, FetchFailure)
        assert result.error_kind in {ErrorKind.READ_TIMEOUT, ErrorKind.CONN_REFUSED}


class TestSsrf:
    """The browser cannot be IP-pinned, so the guard runs at two other points."""

    async def test_a_blocked_main_frame_never_becomes_a_navigation(self, server) -> None:  # type: ignore[no-untyped-def]
        """Checked before Chromium is told the URL at all."""
        strict = BrowserFetcher(SsrfGuard(SystemResolver()), max_pages=1)
        try:
            result = await strict.fetch(request_for(server.url("/")))
            assert isinstance(result, FetchFailure)
            assert result.error_kind is ErrorKind.SSRF_REJECT
        finally:
            await strict.aclose()

    async def test_a_private_subresource_is_aborted(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        """The attack the guard exists for arrives as a subresource, not as a
        navigation: a page that asks the browser to fetch a metadata endpoint
        and post the answer back."""
        result = await fetcher.fetch(request_for(server.url("/js/ssrf")))
        assert isinstance(result, FetchResponse)

        # Read the rendered element, not the raw body: the marker strings also
        # appear in the script that would set them, so a substring search over
        # the whole document passes whatever happened.
        from selectolax.lexbor import LexborHTMLParser

        out = LexborHTMLParser(result.body.decode()).css_first("#out")
        assert out is not None
        assert out.text(strip=True) == "REFUSED", out.text(strip=True)

    async def test_a_subresource_after_load_is_still_guarded(self, server) -> None:  # type: ignore[no-untyped-def]
        """The regression that a flaky test exposed.

        The route handler was removed as soon as ``goto`` returned, so a page
        that waited a moment before reaching for a private address got its
        answer with nothing watching - and scrolling, which happens after
        navigation, ran entirely unguarded.
        """
        from selectolax.lexbor import LexborHTMLParser

        f = BrowserFetcher(loopback_guard(), max_pages=1, settle_ms=2000)
        try:
            result = await f.fetch(request_for(server.url("/js/late-ssrf")))
            assert isinstance(result, FetchResponse)
            out = LexborHTMLParser(result.body.decode()).css_first("#out")
            assert out is not None
            assert out.text(strip=True) == "REFUSED", out.text(strip=True)
        finally:
            await f.aclose()

    async def test_a_file_url_is_refused(self, fetcher) -> None:  # type: ignore[no-untyped-def]
        """`file://` would read this machine's disk into a page the crawled
        site controls. Refused at normalisation, before any browser sees it."""
        from crwallm.policy.url import UrlNormalizationError

        with pytest.raises(UrlNormalizationError):
            request_for("file:///etc/passwd")


class TestResourceBlocking:
    async def test_images_and_fonts_are_not_requested(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        """Most of a page's bytes and none of its data. This is the single
        largest saving the browser path has."""
        await fetcher.fetch(request_for(server.url("/js/heavy")))
        for url in fetcher.last_requests.urls:
            assert not url.endswith((".png", ".jpg", ".woff2"))

    def test_scripts_are_never_blocked(self) -> None:
        """Blocking them would defeat the only reason to open a browser."""
        assert "script" not in BLOCKED_RESOURCE_TYPES

    def test_xhr_is_never_blocked(self) -> None:
        """Those *are* the content, and watching them is how the API behind a
        page gets found."""
        assert "xhr" not in BLOCKED_RESOURCE_TYPES
        assert "fetch" not in BLOCKED_RESOURCE_TYPES


class TestNetworkObservation:
    async def test_an_xhr_the_page_made_is_recorded(self, server) -> None:  # type: ignore[no-untyped-def]
        """Phase 6 finds endpoints a site declares; this finds the ones it
        merely uses. An XHR seen once here can be called directly forever
        after, twenty times faster than the browser that found it.

        ``settle_ms`` is what makes this deterministic. Without it the test
        asserts a race - navigation returns at ``domcontentloaded`` and the
        page's own fetch may not have been issued yet, which is exactly how
        it passed alone and failed in a full run.
        """
        # A budget, not a fixed wait: the fetch returns as soon as the page
        # goes quiet, so a generous ceiling costs nothing when it is quick
        # and still holds up when the machine is loaded by a full test run.
        f = BrowserFetcher(loopback_guard(), max_pages=1, settle_ms=3000)
        try:
            await f.fetch(request_for(server.url("/js/rendered")))
            assert any("/api/" in url for url in f.last_requests.urls)
        finally:
            await f.aclose()

    async def test_settling_is_on_by_default(self) -> None:
        """It was zero, on the reasoning that a crawl not hunting for
        endpoints should not pay the wait. Measurement overruled that: with
        no settle an XHR-driven page rendered 2 times in 5, which makes the
        browser worse than useless - ``auto`` reads an empty render as "the
        page really is empty".
        """
        f = BrowserFetcher(loopback_guard())
        assert f._settle_ms >= 400
        await f.aclose()

    async def test_a_caller_can_still_turn_it_off(self) -> None:
        """For a crawl of pages known to be server-rendered, where the
        browser is doing something other than waiting for scripts."""
        f = BrowserFetcher(loopback_guard(), settle_ms=0)
        assert f._settle_ms == 0
        await f.aclose()

    async def test_each_fetch_starts_a_fresh_record(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        """Otherwise the list grows for the life of the crawl and stops
        answering "what did *this* page call".

        Asserted as "a page with no XHR records none" rather than by comparing
        counts: how many of a page's requests fire before
        ``domcontentloaded`` is a timing question, and an assertion that
        depends on it passes alone and fails in a full run.
        """
        await fetcher.fetch(request_for(server.url("/js/rendered")))
        await fetcher.fetch(request_for(server.url("/a")))
        assert fetcher.last_requests.urls == []


class TestPagePool:
    async def test_pages_are_reused_across_fetches(self, server) -> None:  # type: ignore[no-untyped-def]
        """A page per fetch would leak one per page of a crawl. The budget is
        the ceiling, and staying under it across many fetches is the proof."""
        f = BrowserFetcher(loopback_guard(), max_pages=2)
        try:
            for _ in range(5):
                await f.fetch(request_for(server.url("/a")))
            assert f._made_pages <= 2
        finally:
            await f.aclose()

    async def test_closing_is_idempotent(self, server) -> None:  # type: ignore[no-untyped-def]
        """A crawl that fails mid-flight closes on the way out of two
        different handlers."""
        f = BrowserFetcher(loopback_guard(), max_pages=1)
        await f.fetch(request_for(server.url("/a")))
        await f.aclose()
        await f.aclose()

    async def test_a_closed_fetcher_refuses_rather_than_relaunching(self, server) -> None:  # type: ignore[no-untyped-def]
        f = BrowserFetcher(loopback_guard(), max_pages=1)
        await f.aclose()
        result = await f.fetch(request_for(server.url("/a")))
        assert isinstance(result, FetchFailure)

    async def test_nothing_is_launched_until_something_is_fetched(self) -> None:
        """An ``auto`` crawl that never escalates must never pay for Chromium."""
        f = BrowserFetcher(loopback_guard())
        assert f._browser is None
        await f.aclose()


class TestScrolling:
    async def test_scrolling_off_by_default_leaves_the_page_alone(self, fetcher, server) -> None:  # type: ignore[no-untyped-def]
        result = await fetcher.fetch(request_for(server.url("/js/infinite")))
        assert isinstance(result, FetchResponse)
        assert _items(result) == 10

    async def test_scrolling_loads_more(self, server) -> None:  # type: ignore[no-untyped-def]
        f = BrowserFetcher(
            loopback_guard(),
            max_pages=1,
            scroll=ScrollPolicy(max_rounds=3, pause_ms=120, selector="li.item"),
        )
        try:
            result = await f.fetch(request_for(server.url("/js/infinite")))
            assert isinstance(result, FetchResponse)
            assert _items(result) > 10
        finally:
            await f.aclose()

    async def test_a_finite_page_stops_early(self, server) -> None:  # type: ignore[no-untyped-def]
        """``stop_when_no_growth`` is what keeps a twenty-round budget from
        costing twenty rounds on a page with three screens of content."""
        import time

        f = BrowserFetcher(
            loopback_guard(),
            max_pages=1,
            scroll=ScrollPolicy(max_rounds=20, pause_ms=150, selector="div.quote"),
        )
        try:
            started = time.perf_counter()
            await f.fetch(request_for(server.url("/a")))
            assert time.perf_counter() - started < 20 * 0.15
        finally:
            await f.aclose()
