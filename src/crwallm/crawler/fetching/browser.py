"""Rendering a page when reading it is not enough.

**The browser is the last resort, and the design says so at every level.** It
costs twenty to fifty times an HTTP fetch, so nothing here tries to be fast in
the way the HTTP path is - it tries to be *rare*, and to be cheap when it does
run. Every choice below follows from that (docs/04_CRAWLING_ARCHITECTURE.md).

**One browser, reused contexts, a pool of pages.** Launching Chromium takes
about a second and a fresh context about ten milliseconds. A fetcher that
launched per page would spend more time starting browsers than rendering.

**Subresources are blocked, not merely ignored.** Images, media, fonts and
stylesheets are most of a page's bytes and none of its data. Blocking them at
the route layer is the single largest saving available, and it is why
Playwright is used directly rather than through a wrapper: route interception
is exactly the control a wrapper takes away.

**``domcontentloaded``, never ``networkidle``.** A page with a polling widget
or an open websocket is never idle, and waiting for it means waiting for the
timeout on every such page. The content is in the DOM long before the network
goes quiet.

**SSRF: validate the frame, refuse the rest.** The HTTP fetcher resolves once
and connects to the address it checked. A browser does its own DNS and cannot
be pinned that way, so the guard is applied differently: the main frame's URL
is validated before navigation *and* every request the page makes is checked
against the scope, with anything resolving privately refused outright
(docs/11_SECURITY_MODEL.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from crwallm.crawler.contracts import FetchFailure, FetchRequest, FetchResponse
from crwallm.policy.ssrf import SsrfBlockedError, SsrfGuard
from crwallm.policy.url import NormalizedUrl, UrlNormalizationError, normalize
from crwallm.schemas.types import ErrorKind, FetchMode

if TYPE_CHECKING:  # pragma: no cover - import cost, not behaviour
    from playwright.async_api import Browser, BrowserContext, Page, Request, Route

log = logging.getLogger(__name__)

__all__ = [
    "BLOCKED_RESOURCE_TYPES",
    "BrowserFetcher",
    "BrowserUnavailableError",
    "ScrollPolicy",
    "SeenRequests",
]

BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})
"""Most of a page's bytes and none of its data.

Not ``script``: the whole reason for using a browser is that a script writes
the content. Not ``xhr`` or ``fetch`` either - those *are* the content, and
watching them is how the API behind a page gets found."""

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

LAUNCH_ARGS = (
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
)
"""Chromium flags that remove work, not protection.

Notably absent is ``--no-sandbox``. It is the first suggestion on every forum
and it turns a renderer bug on a hostile page into code execution as this
user, which is precisely the threat a crawler runs into
(docs/11_SECURITY_MODEL.md)."""


class BrowserUnavailableError(RuntimeError):
    """Playwright or its Chromium is not installed.

    A distinct error because the remedy is a command, not a code change, and
    a crawl that hits this should say so rather than reporting the site as
    broken."""


@dataclass(frozen=True, slots=True)
class ScrollPolicy:
    """How far to chase content that loads on scroll.

    Bounded twice on purpose. ``max_rounds`` stops an infinite feed, and
    ``stop_when_no_growth`` stops a finite one early rather than scrolling a
    fixed number of times past the end.
    """

    max_rounds: int = 0
    """Zero means do not scroll, which is the default: most pages do not need
    it and every round costs a wait."""

    pause_ms: int = 700
    stop_when_no_growth: bool = True
    selector: str | None = None
    """What to count between rounds. The container's members when known,
    otherwise page height - which also grows when a footer lazy-loads."""


@dataclass(slots=True)
class SeenRequests:
    """The API calls a page made while rendering.

    Kept because it answers a question nothing else can. Phase 6 finds
    endpoints a site *declares*; this finds the ones it merely uses, and an
    XHR seen once here can be called directly forever after - twenty times
    faster than the browser that found it
    (docs/06_EXTRACTION_ARCHITECTURE.md).

    Best-effort by construction. Navigation returns at ``domcontentloaded``,
    so what lands here is whatever the page had issued by then - a request
    fired from a later callback is simply missed. Waiting for the network to
    settle would catch more and would cost the timeout on every page with a
    polling widget, which is the trade this whole module refuses to make.
    """

    urls: list[str] = field(default_factory=list)
    json_urls: list[str] = field(default_factory=list)

    def note(self, url: str, resource_type: str, content_type: str | None) -> None:
        if resource_type not in {"xhr", "fetch"}:
            return
        if url in self.urls:
            return
        self.urls.append(url)
        if content_type and "json" in content_type.lower():
            self.json_urls.append(url)


class BrowserFetcher:
    """``Fetcher`` over Playwright's Chromium.

    Lazily started: constructing one is free, so a crawl configured for
    ``auto`` pays nothing for the browser it may never use.
    """

    def __init__(
        self,
        guard: SsrfGuard,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        headless: bool = True,
        max_pages: int = 4,
        block_resources: bool = True,
        scroll: ScrollPolicy | None = None,
        allow_private_subresources: bool = False,
    ) -> None:
        self._guard = guard
        self._user_agent = user_agent
        self._headless = headless
        self._block = block_resources
        self._scroll = scroll or ScrollPolicy()
        self._allow_private = allow_private_subresources

        self._playwright: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pages: asyncio.Queue[Page] = asyncio.Queue()
        self._page_budget = max_pages
        self._made_pages = 0
        self._lock = asyncio.Lock()
        self._closed = False

        self.last_requests = SeenRequests()

    # ------------------------------------------------------------- lifecycle

    async def _ensure_started(self) -> BrowserContext:
        """Launch once, under a lock.

        Several workers reach this at the same moment on the first browser
        page of a crawl, and two of them launching Chromium is a second of
        wasted work and a leaked process.
        """
        async with self._lock:
            if self._context is not None:
                return self._context
            if self._closed:
                raise BrowserUnavailableError("fetcher is closed")

            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - depends on install
                raise BrowserUnavailableError(
                    "playwright is not installed - run: uv pip install playwright"
                ) from exc

            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=self._headless,
                    args=list(LAUNCH_ARGS),
                )
            except Exception as exc:
                await self._shutdown_playwright()
                raise BrowserUnavailableError(
                    f"could not launch Chromium ({exc}). Run: python -m playwright install chromium"
                ) from exc

            self._context = await self._browser.new_context(
                user_agent=self._user_agent,
                java_script_enabled=True,
                accept_downloads=False,
                bypass_csp=False,
                locale="ko-KR",
            )
            # Belt and braces: `accept_downloads=False` covers the negotiated
            # case, and a page that starts one anyway is cancelled here.
            self._context.on("download", lambda d: asyncio.ensure_future(d.cancel()))
            return self._context

    async def _acquire_page(self) -> Page:
        context = await self._ensure_started()
        with contextlib.suppress(asyncio.QueueEmpty):
            return self._pages.get_nowait()

        async with self._lock:
            if self._made_pages < self._page_budget:
                self._made_pages += 1
                return await context.new_page()

        # At the budget: wait for one to come back rather than exceeding it.
        return await self._pages.get()

    async def _release_page(self, page: Page) -> None:
        """Return a page to the pool, or discard it if it is no longer usable.

        A page whose renderer crashed will fail every future navigation, and
        putting it back would poison the pool for the rest of the crawl.
        """
        if self._closed or page.is_closed():
            async with self._lock:
                self._made_pages -= 1
            return
        try:
            # Cheap reset: leaving the last site's document loaded keeps its
            # timers running while the page sits in the pool.
            await page.goto("about:blank", wait_until="commit", timeout=5_000)
        except Exception:
            with contextlib.suppress(Exception):
                await page.close()
            async with self._lock:
                self._made_pages -= 1
            return
        await self._pages.put(page)

    async def _shutdown_playwright(self) -> None:
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    async def aclose(self) -> None:
        """Deterministic cleanup.

        Chromium does not exit when its Python object is collected; a fetcher
        that is not closed leaves a process behind for every crawl.
        """
        self._closed = True
        while not self._pages.empty():
            page = self._pages.get_nowait()
            with contextlib.suppress(Exception):
                await page.close()
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
            self._context = None
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None
        await self._shutdown_playwright()

    # ----------------------------------------------------------- navigation

    async def fetch(self, request: FetchRequest) -> FetchResponse | FetchFailure:
        """Render one page and return its HTML.

        The main frame's URL goes through the guard before Chromium is told
        about it, so a blocked address never becomes a navigation.
        """
        try:
            await self._guard.check(request.url)
        except SsrfBlockedError as exc:
            return FetchFailure(
                url=request.url,
                error_kind=ErrorKind.SSRF_REJECT,
                message=str(exc),
            )

        started = time.perf_counter()
        seen = SeenRequests()

        try:
            page = await self._acquire_page()
        except BrowserUnavailableError as exc:
            return FetchFailure(
                url=request.url,
                error_kind=ErrorKind.INTERNAL,
                message=str(exc),
                retryable=False,
            )

        try:
            return await self._render(page, request, seen, started)
        except Exception as exc:
            log.debug("browser fetch failed for %s: %s", request.url.url, exc)
            return FetchFailure(
                url=request.url,
                error_kind=_classify(exc),
                message=f"{type(exc).__name__}: {exc}",
                retryable=True,
            )
        finally:
            self.last_requests = seen
            await self._release_page(page)

    async def _render(
        self,
        page: Page,
        request: FetchRequest,
        seen: SeenRequests,
        started: float,
    ) -> FetchResponse | FetchFailure:
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        scope = _registrable(request.url.url)
        handler = _RouteGuard(
            guard=self._guard,
            scope=scope,
            block_resources=self._block,
            allow_private=self._allow_private,
            seen=seen,
        )
        await page.route("**/*", handler)

        try:
            response = await page.goto(
                request.url.url,
                wait_until="domcontentloaded",
                timeout=request.timeout_s * 1000,
            )
        except PlaywrightTimeout:
            return FetchFailure(
                url=request.url,
                error_kind=ErrorKind.READ_TIMEOUT,
                message=f"navigation exceeded {request.timeout_s}s",
                retryable=True,
            )
        finally:
            with contextlib.suppress(Exception):
                await page.unroute("**/*", handler)

        if response is None:
            return FetchFailure(
                url=request.url,
                error_kind=ErrorKind.CONN_REFUSED,
                message="navigation produced no response",
                retryable=True,
            )

        if self._scroll.max_rounds:
            await _scroll_for_more(page, self._scroll)

        html = await page.content()
        body = html.encode("utf-8")
        if len(body) > request.byte_limit:
            return FetchFailure(
                url=request.url,
                error_kind=ErrorKind.SIZE_EXCEEDED,
                message=f"rendered {len(body)} bytes over limit {request.byte_limit}",
            )

        final_url = page.url
        headers = {k.lower(): v for k, v in (await response.all_headers()).items()}
        # The rendered document is UTF-8 whatever the source declared: this is
        # `page.content()`, which is serialised from the parsed DOM.
        headers["content-type"] = "text/html; charset=utf-8"

        return FetchResponse(
            url=request.url,
            status=response.status,
            headers=headers,
            body=body,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            fetch_mode=FetchMode.BROWSER,
            final_url=final_url if final_url != request.url.url else None,
        )


# ---------------------------------------------------------------- routing


class _RouteGuard:
    """Decides what the page is allowed to request.

    A callable class rather than a closure so it can be passed to ``unroute``
    by identity - Playwright matches handlers on the object, and an anonymous
    lambda cannot be removed, which leaks a handler per fetch.

    No ``__slots__``: Playwright annotates the handler it is given with its
    own attribute, and a slotted class refuses that with an ``AttributeError``
    from inside ``page.route``. One instance is made per fetch, so there was
    nothing to save.
    """

    def __init__(
        self,
        *,
        guard: SsrfGuard,
        scope: str,
        block_resources: bool,
        allow_private: bool,
        seen: SeenRequests,
    ) -> None:
        self._guard = guard
        self._scope = scope
        self._block = block_resources
        self._allow_private = allow_private
        self._seen = seen

    async def __call__(self, route: Route, request: Request) -> None:
        url = request.url
        resource_type = request.resource_type

        # Anything that is not the web. `file://` would read this machine's
        # disk into a page the crawled site controls.
        scheme = urlsplit(url).scheme.lower()
        if scheme not in {"http", "https", "data", "about", "blob"}:
            await _abort(route)
            return

        if self._block and resource_type in BLOCKED_RESOURCE_TYPES:
            await _abort(route)
            return

        if scheme in {"http", "https"} and not self._allow_private:
            try:
                await self._guard.check(url)
            except SsrfBlockedError:
                # A page asking the browser to fetch 169.254.169.254 is the
                # attack the guard exists for, and it arrives as a subresource
                # rather than as a navigation.
                await _abort(route)
                return
            except UrlNormalizationError:
                await _abort(route)
                return

        self._seen.note(url, resource_type, request.headers.get("accept"))

        # The page can navigate away mid-flight, which makes the route
        # object stale; there is nothing to do about it and nothing lost.
        with contextlib.suppress(Exception):
            await route.continue_()


async def _abort(route: Route) -> None:
    with contextlib.suppress(Exception):
        await route.abort()


# ------------------------------------------------------------------ scroll


async def _scroll_for_more(page: Page, policy: ScrollPolicy) -> None:
    """Scroll until the page stops growing, or the budget runs out.

    Worth saying plainly: this is the expensive way to do it. An infinite feed
    loads by calling an XHR, and calling that XHR directly is roughly twenty
    times faster - which is what the recorded requests are for. This exists
    for the pages where that call cannot be reconstructed.
    """
    previous = await _measure(page, policy.selector)

    for _ in range(policy.max_rounds):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(policy.pause_ms)

        current = await _measure(page, policy.selector)
        if policy.stop_when_no_growth and current <= previous:
            return
        previous = current


async def _measure(page: Page, selector: str | None) -> int:
    """How much there is now.

    Counting the container's members is the honest measure when one is known.
    Page height stands in otherwise, and it moves for reasons other than new
    content - a lazy footer, an expanding banner - which is why the count is
    preferred whenever the caller can name a selector.
    """
    if selector:
        with contextlib.suppress(Exception):
            return int(await page.evaluate(f"document.querySelectorAll({selector!r}).length"))
    with contextlib.suppress(Exception):
        return int(await page.evaluate("document.body.scrollHeight"))
    return 0


# ------------------------------------------------------------------ helpers


def _registrable(url: str) -> str:
    from crwallm.policy.domains import registrable_domain

    host = urlsplit(url).hostname or ""
    try:
        return registrable_domain(host) or host
    except Exception:
        return host


def _classify(exc: Exception) -> ErrorKind:
    text = str(exc).lower()
    if "timeout" in text:
        return ErrorKind.READ_TIMEOUT
    if "err_name_not_resolved" in text or "dns" in text:
        return ErrorKind.DNS_FAIL
    if "err_connection_refused" in text:
        return ErrorKind.CONN_REFUSED
    if "err_cert" in text or "ssl" in text:
        return ErrorKind.TLS_ERROR
    return ErrorKind.INTERNAL


def _normalized(url: str) -> NormalizedUrl | None:
    with contextlib.suppress(UrlNormalizationError):
        return normalize(url)
    return None
