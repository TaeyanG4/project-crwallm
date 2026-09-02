"""A server that behaves badly on purpose.

Written alongside the SSRF and trap code, not after it. The distinction
matters: ``classify_address`` looks correct on inspection, but only a real
302 to ``169.254.169.254`` proves that the *redirect path* consults it. Tests
retrofitted onto finished code get written to match whatever the code already
does.

Every endpoint below models a way real crawlers die.
docs/11_SECURITY_MODEL.md, docs/05_SPIDER_ARCHITECTURE.md
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse

# Addresses a crawler must never be talked into reaching.
LOOPBACK = "127.0.0.1"
METADATA_V4 = "169.254.169.254"
PRIVATE_V4 = "192.168.1.1"
MAPPED_LOOPBACK = "[::ffff:127.0.0.1]"


def create_app() -> FastAPI:
    app = FastAPI(title="malicious-fixture", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------ benign
    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        """A normal page, so tests can prove the fetcher works at all."""
        return (
            "<html><head><title>ok</title>"
            '<link rel="canonical" href="/canonical-target">'
            "</head><body>"
            '<a href="/a">a</a><a href="/b">b</a><a href="/">self</a>'
            "</body></html>"
        )

    @app.get("/canonical-target", response_class=HTMLResponse)
    async def canonical_target() -> str:
        return "<html><body>canonical</body></html>"

    @app.get("/a", response_class=HTMLResponse)
    async def page_a() -> str:
        return '<html><body><a href="/b">b</a></body></html>'

    @app.get("/b", response_class=HTMLResponse)
    async def page_b() -> str:
        return "<html><body>b</body></html>"

    @app.get("/shop", response_class=HTMLResponse)
    async def shop(page: int = 1) -> str:
        """A listing page shaped like a real one.

        Deliberately awkward in the ways real pages are: framework layout
        classes on every card, a navigation menu that also repeats, one item
        with no price, lazy-loaded images with a placeholder in ``src``, and a
        button whose text is identical on every row. The structure detector has
        to find the grid through all of that.
        """
        start = (page - 1) * 4 + 1
        cards = "".join(
            f'<li class="product-item col-md-3 col-sm-6" data-idx="{i}">'
            f'  <div class="card-body p-2">'
            f'    <h3 class="name">'
            f'<a href="/shop/item/{i}">Laptop model {i} with a long name</a></h3>'
            f'    <span class="price">{i}90,000</span>'
            f'    <img data-src="/img/{i}.jpg" src="/img/blank.gif" alt="pic">'
            f'    <button class="btn btn-cart">Add to cart</button>'
            f"  </div></li>"
            if i % 4 != 0
            else (
                f'<li class="product-item col-md-3 col-sm-6" data-idx="{i}">'
                f'  <div class="card-body p-2">'
                f'    <h3 class="name">'
                f'<a href="/shop/item/{i}">Laptop model {i} sold out edition</a></h3>'
                f'    <span class="price">Sold out</span>'
                f'    <img data-src="/img/{i}.jpg" src="/img/blank.gif" alt="pic">'
                f'    <button class="btn btn-cart">Add to cart</button>'
                f"  </div></li>"
            )
            for i in range(start, start + 8)
        )
        nav = "".join(
            f'<li class="nav-item"><a href="/shop?cat={c}">{c}</a></li>'
            for c in ("new", "sale", "brands", "help")
        )
        nxt = (
            f'<a class="pagination-next" href="/shop?page={page + 1}">next</a>' if page < 3 else ""
        )
        return (
            f"<html><head><title>Shop page {page}</title>"
            f'<link rel="canonical" href="/shop?page={page}"></head><body>'
            f'<nav><ul class="menu">{nav}</ul></nav>'
            f'<main><ul class="grid row">{cards}</ul>{nxt}</main>'
            f"<footer><p>copyright</p></footer></body></html>"
        )

    @app.get("/shop/item/{n}", response_class=HTMLResponse)
    async def shop_item(n: int) -> str:
        """A detail page - no repetition, so the detector should say so."""
        return (
            f"<html><head><title>Laptop {n}</title></head><body>"
            f'<h1 class="title">Laptop model {n}</h1>'
            f'<div class="spec"><span class="price">{n}90,000</span>'
            f'<p class="desc">A description of laptop {n} that runs to a sentence.</p></div>'
            f"</body></html>"
        )

    # ------------------------------------------------------------- SSRF
    # Each of these is a link a crawled page could plausibly contain.

    @app.get("/redirect/loopback")
    async def redirect_loopback() -> RedirectResponse:
        """The basic one: a public URL that bounces to localhost."""
        return RedirectResponse(f"http://{LOOPBACK}:9/internal", status_code=302)

    @app.get("/redirect/metadata")
    async def redirect_metadata() -> RedirectResponse:
        """Cloud credential theft, one hop away."""
        return RedirectResponse(
            f"http://{METADATA_V4}/latest/meta-data/iam/security-credentials/",
            status_code=302,
        )

    @app.get("/redirect/private")
    async def redirect_private() -> RedirectResponse:
        return RedirectResponse(f"http://{PRIVATE_V4}/admin", status_code=302)

    @app.get("/redirect/mapped")
    async def redirect_mapped() -> RedirectResponse:
        """IPv4-mapped IPv6 - loopback in a costume."""
        return RedirectResponse(f"http://{MAPPED_LOOPBACK}:9/internal", status_code=302)

    @app.get("/redirect/scheme")
    async def redirect_scheme() -> RedirectResponse:
        """Scheme downgrade to something that reads the filesystem."""
        return RedirectResponse("file:///etc/passwd", status_code=302)

    @app.get("/redirect/chain/{n}")
    async def redirect_chain(n: int) -> RedirectResponse:
        """``redirect_max`` - a long but finite chain."""
        if n <= 0:
            return RedirectResponse("/", status_code=302)
        return RedirectResponse(f"/redirect/chain/{n - 1}", status_code=302)

    @app.get("/redirect/loop")
    async def redirect_loop() -> RedirectResponse:
        return RedirectResponse("/redirect/loop", status_code=302)

    @app.get("/redirect/pingpong/{side}")
    async def redirect_pingpong(side: str) -> RedirectResponse:
        """Two-URL cycle - a hop counter catches it, a visited-set catches it
        sooner."""
        other = "pong" if side == "ping" else "ping"
        return RedirectResponse(f"/redirect/pingpong/{other}", status_code=302)

    # -------------------------------------------------------- resources

    @app.get("/huge")
    async def huge() -> StreamingResponse:
        """Never ends, never sends Content-Length.

        The byte limit has to bite *during* streaming; a limit checked against
        the header does nothing here.
        """

        async def body() -> AsyncIterator[bytes]:
            chunk = b"A" * 8192
            while True:
                yield chunk
                await asyncio.sleep(0)

        return StreamingResponse(body(), media_type="text/html")

    @app.get("/huge-lying-header")
    async def huge_lying_header() -> StreamingResponse:
        """Claims to be tiny, streams forever."""

        async def body() -> AsyncIterator[bytes]:
            while True:
                yield b"B" * 8192
                await asyncio.sleep(0)

        return StreamingResponse(body(), media_type="text/html", headers={"Content-Length": "10"})

    @app.get("/slow")
    async def slow() -> StreamingResponse:
        """Headers now, body never. Read timeout, not connect timeout."""

        async def body() -> AsyncIterator[bytes]:
            yield b"<html><body>"
            await asyncio.sleep(3600)
            yield b"</body></html>"  # pragma: no cover

        return StreamingResponse(body(), media_type="text/html")

    @app.get("/slow-headers")
    async def slow_headers() -> Response:
        await asyncio.sleep(3600)
        return PlainTextResponse("never")  # pragma: no cover

    # ------------------------------------------------------------ traps

    @app.get("/calendar/{year}/{month}", response_class=HTMLResponse)
    async def calendar(year: int, month: int) -> str:
        """Infinite by construction. Every page links to the next.

        Without a per-pattern budget this alone consumes the crawl.
        """
        nxt = (year + 1, 1) if month >= 12 else (year, month + 1)
        prv = (year - 1, 12) if month <= 1 else (year, month - 1)
        return (
            f"<html><body><h1>{year}-{month:02d}</h1>"
            f'<a href="/calendar/{nxt[0]}/{nxt[1]:02d}">next</a>'
            f'<a href="/calendar/{prv[0]}/{prv[1]:02d}">prev</a>'
            "</body></html>"
        )

    @app.get("/facet", response_class=HTMLResponse)
    async def facet(request: Request) -> str:
        """Faceted navigation: every page offers every remaining combination.

        Combinatorial explosion. The query whitelist is what stops it.
        """
        params = dict(request.query_params)
        links = "".join(
            f'<a href="/facet?{"&".join(f"{k}={v}" for k, v in {**params, name: value}.items())}">'
            f"{name}={value}</a>"
            for name in ("color", "size", "brand", "sort", "page")
            for value in ("a", "b", "c")
        )
        return f"<html><body>{links}</body></html>"

    @app.get("/session/{token}/page", response_class=HTMLResponse)
    async def session_page(token: str) -> str:
        """A fresh session id on every link, so naive dedupe never fires."""
        nxt = hashlib.sha256(token.encode()).hexdigest()[:16]
        return f'<html><body><a href="/session/{nxt}/page">next</a></body></html>'

    @app.get("/deep/{path:path}", response_class=HTMLResponse)
    async def deep(path: str) -> str:
        """``/deep/a/b/a/b/...`` - a router that accepts its own prefix."""
        segments = [s for s in path.split("/") if s]
        nxt = "/".join([*segments, "a", "b"])
        return f'<html><body><a href="/deep/{nxt}">deeper</a></body></html>'

    @app.get("/soft404", response_class=HTMLResponse)
    async def soft404() -> str:
        """200 OK, "not found" body. Status codes alone will not save you."""
        return "<html><body><h1>Page not found</h1>The page does not exist.</body></html>"

    @app.get("/duplicate/{n}", response_class=HTMLResponse)
    async def duplicate(n: int) -> str:
        """Distinct URLs, identical content. Only content hashing catches it."""
        return "<html><body><p>Exactly the same every time.</p></body></html>"

    # ------------------------------------------------------- http status

    @app.get("/status/{code}")
    async def status(code: int) -> Response:
        return PlainTextResponse(f"status {code}", status_code=code)

    @app.get("/ratelimit")
    async def ratelimit() -> Response:
        """429 with Retry-After - the adaptive controller's input."""
        return PlainTextResponse("slow down", status_code=429, headers={"Retry-After": "2"})

    @app.get("/encoded/{scheme}")
    async def encoded(scheme: str) -> Response:
        """A normal page, compressed.

        Advertising an encoding the client cannot decode does not error - it
        returns 200 with a body of noise, and every record from that page is
        silently wrong. This endpoint is what proves the fetcher only asks for
        what it can actually read.
        """
        payload = (
            "<html><head><title>compressed</title></head><body>테스트</body></html>"
        ).encode()

        if scheme == "gzip":
            import gzip as gzip_mod

            body = gzip_mod.compress(payload)
        elif scheme == "deflate":
            import zlib

            body = zlib.compress(payload)
        elif scheme == "br":
            try:
                import brotli
            except ImportError:  # pragma: no cover - the dependency is declared
                return PlainTextResponse("brotli unavailable", status_code=501)
            body = brotli.compress(payload)
        else:
            return PlainTextResponse(f"unknown encoding {scheme}", status_code=400)

        return Response(content=body, media_type="text/html", headers={"Content-Encoding": scheme})

    @app.get("/gzip-bomb")
    async def gzip_bomb() -> Response:
        """Small compressed, enormous decompressed.

        The byte limit must count *decompressed* bytes.
        """
        import gzip

        payload = gzip.compress(b"\0" * (64 * 1024 * 1024))
        return Response(
            content=payload,
            media_type="text/html",
            headers={"Content-Encoding": "gzip"},
        )

    # ----------------------------------------------------------- headers

    @app.get("/header-injection", response_class=HTMLResponse)
    async def header_injection() -> str:
        """A link with an embedded newline - header smuggling via crawled HTML."""
        return '<html><body><a href="/ok\r\nX-Injected: 1">click</a></body></html>'

    return app
