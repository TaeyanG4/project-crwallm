"""The HTTP fetcher, against the adversarial fixture.

Every test here corresponds to a way a crawler dies or leaks. They run over a
real socket because the behaviour under test - streaming, timeouts, abandoned
connections, redirect hops - does not exist in an ASGI transport.

docs/04_CRAWLING_ARCHITECTURE.md, docs/11_SECURITY_MODEL.md
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Iterator

import pytest

from crwallm.crawler.contracts import FetchFailure, FetchRequest, FetchResponse
from crwallm.crawler.fetching.http import DEFAULT_USER_AGENT, SafeHttpFetcher
from crwallm.policy.ssrf import SsrfGuard, StaticResolver
from crwallm.policy.url import normalize
from crwallm.schemas.types import ErrorKind, FetchMode
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
    """Allowed to reach the loopback fixture and nothing else internal.

    ``allow_networks`` is the documented test-only escape hatch
    (docs/11_SECURITY_MODEL.md); the tests below check it stays scoped to
    127/8.
    """
    guard = SsrfGuard(
        StaticResolver(  # type: ignore[arg-type]
            {
                "public.test": ["93.184.216.34"],
                "meta.test": ["169.254.169.254"],
            }
        ),
        allow_networks=LOOPBACK,
    )
    f = SafeHttpFetcher(guard, http2=False)
    try:
        yield f
    finally:
        await f.aclose()


def request(
    url: str, *, timeout_s: float = 5.0, byte_limit: int = 200_000, max_redirects: int = 5
) -> FetchRequest:
    return FetchRequest(
        url=normalize(url),
        depth=0,
        mode=FetchMode.HTTP,
        timeout_s=timeout_s,
        byte_limit=byte_limit,
        max_redirects=max_redirects,
    )


class TestHappyPath:
    async def test_fetches_a_page(self, fetcher: SafeHttpFetcher, server: RunningServer) -> None:
        out = await fetcher.fetch(request(server.url("/")))
        assert isinstance(out, FetchResponse)
        assert out.status == 200
        assert b"<title>ok</title>" in out.body
        assert out.content_type == "text/html"
        assert out.elapsed_ms >= 0

    async def test_sends_an_identifiable_user_agent(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        """Politeness rules are relaxed here, but impersonating a browser is a
        separate choice and docs/17_NON_GOALS.md rules it out."""
        assert "crwallm" in DEFAULT_USER_AGENT
        assert "Mozilla" not in DEFAULT_USER_AGENT


class TestPinningIsEnforced:
    async def test_unresolvable_host_never_reaches_the_socket(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(f"http://nowhere.test:{server.port}/"))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.SSRF_REJECT

    async def test_internal_resolution_is_refused(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(f"http://meta.test:{server.port}/"))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.SSRF_REJECT

    async def test_transport_fails_closed_without_a_pin(self) -> None:
        """The guarantee that makes the guard structural rather than a habit.

        A request built without going through the guard must not connect, so
        bypassing the check requires deleting code rather than forgetting it.
        """
        import httpx

        from crwallm.crawler.fetching.pinning import PinnedTransport, PinRegistry

        transport = PinnedTransport(PinRegistry(), http2=False)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ConnectError, match="unpinned host"):
                await client.get("http://127.0.0.1:1/")


class TestRedirects:
    """Hops two onward are the ones that matter.

    ``follow_redirects=True`` would hide every one of these from the guard.
    """

    async def test_redirect_to_metadata_is_refused(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(server.url("/redirect/metadata")))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.SSRF_REJECT

    async def test_redirect_to_private_range_is_refused(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(server.url("/redirect/private")))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.SSRF_REJECT

    async def test_scheme_downgrade_is_refused(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        """A 302 to file:///etc/passwd dies at normalisation, not at the socket."""
        out = await fetcher.fetch(request(server.url("/redirect/scheme")))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.SSRF_REJECT

    async def test_hop_limit_is_enforced(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(server.url("/redirect/chain/9"), max_redirects=3))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.REDIRECT_MAX

    async def test_short_chain_completes(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(server.url("/redirect/chain/2"), max_redirects=5))
        assert isinstance(out, FetchResponse)
        assert out.status == 200
        assert out.redirects == 3

    async def test_self_referential_loop_is_caught_by_identity(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        """A visited set stops this on hop two; a counter would burn the budget."""
        out = await fetcher.fetch(request(server.url("/redirect/loop"), max_redirects=20))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.REDIRECT_LOOP

    async def test_two_url_cycle_is_caught(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(server.url("/redirect/pingpong/ping"), max_redirects=20))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.REDIRECT_LOOP


class TestSizeLimit:
    """Checking Content-Length stops none of these."""

    async def test_endless_stream_without_a_length_header(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(server.url("/huge"), byte_limit=50_000))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.SIZE_EXCEEDED

    async def test_gzip_bomb_is_measured_after_decompression(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        """64MB of zeros, small on the wire. Counting raw bytes would let it
        through and then hold it all in memory."""
        out = await fetcher.fetch(request(server.url("/gzip-bomb"), byte_limit=100_000))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.SIZE_EXCEEDED

    async def test_lying_content_length_is_a_protocol_error(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        """Claims ten bytes, streams forever. The transfer is refused - which
        one of the two error kinds it lands on matters less than the fact that
        nothing unbounded is buffered."""
        out = await fetcher.fetch(request(server.url("/huge-lying-header"), byte_limit=50_000))
        assert isinstance(out, FetchFailure)
        assert out.error_kind in (ErrorKind.PARSE_FAIL, ErrorKind.SIZE_EXCEEDED)

    async def test_a_page_under_the_limit_is_returned_whole(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(server.url("/"), byte_limit=1_000_000))
        assert isinstance(out, FetchResponse)
        assert out.body.endswith(b"</html>")


class TestTimeouts:
    async def test_body_that_never_arrives_times_out(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        """Headers arrive, the body does not. A connect timeout would not fire."""
        out = await fetcher.fetch(request(server.url("/slow"), timeout_s=1.0))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.READ_TIMEOUT
        assert out.retryable

    async def test_headers_that_never_arrive_time_out(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        out = await fetcher.fetch(request(server.url("/slow-headers"), timeout_s=1.0))
        assert isinstance(out, FetchFailure)
        assert out.error_kind in (ErrorKind.READ_TIMEOUT, ErrorKind.CONN_TIMEOUT)


class TestErrorTaxonomy:
    """ "400 pages failed" is not actionable. "380 were blocked_429" is."""

    @pytest.mark.parametrize(
        ("path", "kind", "retryable"),
        [
            ("/status/404", ErrorKind.HTTP_4XX, False),
            ("/status/400", ErrorKind.HTTP_4XX, False),
            ("/status/403", ErrorKind.BLOCKED_403, False),
            ("/status/500", ErrorKind.HTTP_5XX, True),
            ("/status/503", ErrorKind.HTTP_5XX, True),
        ],
    )
    async def test_status_maps_to_a_kind(
        self,
        fetcher: SafeHttpFetcher,
        server: RunningServer,
        path: str,
        kind: ErrorKind,
        retryable: bool,
    ) -> None:
        out = await fetcher.fetch(request(server.url(path)))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is kind
        assert out.retryable is retryable

    async def test_retry_after_is_parsed(
        self, fetcher: SafeHttpFetcher, server: RunningServer
    ) -> None:
        """A host that says how long to wait is worth listening to - the
        alternative is being blocked outright."""
        out = await fetcher.fetch(request(server.url("/ratelimit")))
        assert isinstance(out, FetchFailure)
        assert out.error_kind is ErrorKind.BLOCKED_429
        assert out.retry_after_s == 2.0
        assert out.retryable


class TestRetryAfterParsing:
    def test_delta_seconds(self) -> None:
        from crwallm.crawler.fetching.http import _parse_retry_after

        assert _parse_retry_after("120") == 120.0

    def test_http_date(self) -> None:
        import email.utils
        import time

        from crwallm.crawler.fetching.http import _parse_retry_after

        future = email.utils.formatdate(time.time() + 60, usegmt=True)
        value = _parse_retry_after(future)
        assert value is not None
        assert 50 < value < 70

    def test_past_date_clamps_to_zero(self) -> None:
        import email.utils
        import time

        from crwallm.crawler.fetching.http import _parse_retry_after

        past = email.utils.formatdate(time.time() - 600, usegmt=True)
        assert _parse_retry_after(past) == 0.0

    def test_missing_header(self) -> None:
        from crwallm.crawler.fetching.http import _parse_retry_after

        assert _parse_retry_after(None) is None


class TestContentEncoding:
    """The bug this class exists for.

    ``Accept-Encoding`` advertised brotli while httpx could not decode it
    (the optional dependency was missing). The server complied, the fetch
    returned 200, and the body was compressed bytes stored as HTML. Nothing
    failed - the records were just quietly wrong.
    """

    def test_only_decodable_encodings_are_advertised(self) -> None:
        from httpx._decoders import SUPPORTED_DECODERS

        from crwallm.crawler.fetching.http import supported_encodings

        advertised = {e.strip() for e in supported_encodings().split(",")}
        assert advertised
        assert advertised <= set(SUPPORTED_DECODERS), (
            "advertising an encoding we cannot decode corrupts every page that uses it"
        )

    @pytest.mark.parametrize("scheme", ["gzip", "deflate", "br"])
    async def test_compressed_bodies_arrive_decoded(
        self, fetcher: SafeHttpFetcher, server: RunningServer, scheme: str
    ) -> None:
        out = await fetcher.fetch(request(server.url(f"/encoded/{scheme}")))
        assert isinstance(out, FetchResponse)
        assert out.status == 200
        assert b"<title>compressed</title>" in out.body, f"{scheme} body was not decoded"
        assert "테스트" in out.body.decode("utf-8")
