"""The SSRF guard and trap guards, attacked over a real socket.

The unit tests prove the predicates are correct. These prove the predicates
are *reached* - that a redirect really is re-checked, that a link really is
normalised before the scope test, that a generative URL shape really does hit
its budget. That gap is where SSRF bugs live.

Phase 2 extends this file as the fetcher gains the behaviour each endpoint is
waiting for (streaming byte limits, timeouts, redirect handling). The
endpoints exist now, alongside the policy code, deliberately:
docs/11_SECURITY_MODEL.md
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator

import pytest

from crwallm.policy.domains import DomainScope
from crwallm.policy.ssrf import SsrfBlockedError, SsrfGuard, StaticResolver
from crwallm.policy.traps import TrapGuard
from crwallm.policy.url import normalize
from crwallm.schemas.spec import SpiderConfig
from crwallm.schemas.types import RejectReason
from tests.fixtures.malicious_server.server import MaliciousServer, RunningServer

pytestmark = pytest.mark.integration

LOOPBACK_NET = ipaddress.ip_network("127.0.0.0/8")


@pytest.fixture(scope="module")
def server() -> Iterator[RunningServer]:
    s = MaliciousServer()
    try:
        yield s.start()
    finally:
        s.stop()


@pytest.fixture
def guard() -> SsrfGuard:
    """Guard that may reach the fixture but nothing else internal.

    The allowlist is the documented test-only escape hatch. Note it covers
    127/8 alone: every other blocked range must still be refused, which is what
    the tests below check.
    """
    return SsrfGuard(
        StaticResolver(  # type: ignore[arg-type]
            {
                "public.test": ["93.184.216.34"],
                "evil.test": ["127.0.0.1"],
                "meta.test": ["169.254.169.254"],
                "intranet.test": ["192.168.1.10"],
            }
        ),
        allow_networks=[LOOPBACK_NET],
    )


class TestServerIsReachable:
    async def test_fixture_serves_a_normal_page(self, server: RunningServer) -> None:
        import httpx

        async with httpx.AsyncClient() as client:
            r = await client.get(server.url("/"))
        assert r.status_code == 200
        assert "<title>ok</title>" in r.text


class TestRedirectTargetsAreRefused:
    """Every one of these is a 302 the fetcher will follow in Phase 2.

    The guard has to be consulted on the *target*, not just the seed. Checking
    only the seed is the single most common way SSRF ships.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/redirect/metadata",
            "/redirect/private",
            "/redirect/scheme",
        ],
    )
    async def test_redirect_target_is_blocked(
        self, server: RunningServer, guard: SsrfGuard, path: str
    ) -> None:
        import httpx

        async with httpx.AsyncClient(follow_redirects=False) as client:
            response = await client.get(server.url(path))

        assert response.status_code == 302
        target = response.headers["location"]

        with pytest.raises(SsrfBlockedError):
            await guard.check(target)

    async def test_mapped_loopback_redirect_is_blocked(self, server: RunningServer) -> None:
        """``::ffff:127.0.0.1`` must not ride in on the loopback allowlist -
        the allowlist is an IPv4 network and the address arrives as IPv6."""
        import httpx

        strict = SsrfGuard(StaticResolver({}))  # type: ignore[arg-type]
        async with httpx.AsyncClient(follow_redirects=False) as client:
            response = await client.get(server.url("/redirect/mapped"))

        with pytest.raises(SsrfBlockedError):
            await strict.check(response.headers["location"])

    async def test_redirect_chain_length_is_observable(self, server: RunningServer) -> None:
        """The chain is finite; ``redirect_max`` is what stops it early
        (fetcher, Phase 2). Here we only prove the fixture behaves."""
        import httpx

        async with httpx.AsyncClient(follow_redirects=False) as client:
            r = await client.get(server.url("/redirect/chain/3"))
        assert r.status_code == 302
        assert r.headers["location"].endswith("/2")

    async def test_redirect_loop_is_self_referential(self, server: RunningServer) -> None:
        import httpx

        async with httpx.AsyncClient(follow_redirects=False) as client:
            r = await client.get(server.url("/redirect/loop"))
        assert r.headers["location"].endswith("/redirect/loop")


class TestHostnamesThatResolveInward:
    async def test_public_name_passes(self, guard: SsrfGuard) -> None:
        assert str((await guard.check("http://public.test/")).ip) == "93.184.216.34"

    @pytest.mark.parametrize("host", ["meta.test", "intranet.test"])
    async def test_internal_names_are_refused(self, guard: SsrfGuard, host: str) -> None:
        with pytest.raises(SsrfBlockedError):
            await guard.check(f"http://{host}/")

    async def test_allowlist_is_scoped_to_loopback_only(self, guard: SsrfGuard) -> None:
        """A single allowed range must not become a general amnesty."""
        assert (await guard.check("http://evil.test/")).ip == ipaddress.ip_address("127.0.0.1")
        with pytest.raises(SsrfBlockedError):
            await guard.check("http://intranet.test/")


class TestLinkHandling:
    async def test_injected_newline_never_becomes_a_header(self, server: RunningServer) -> None:
        """A crawled page offers ``/ok\\r\\nX-Injected: 1`` as an href.

        The property under test is that no CR or LF survives normalisation, in
        raw or percent-encoded form, so nothing downstream can be talked into
        starting a new header line. The leftover text collapses into the path -
        browsers do the same, because real HTML wraps long hrefs across lines -
        and costs one 404 in the rare case somebody tries this.
        """
        import httpx

        async with httpx.AsyncClient() as client:
            html = (await client.get(server.url("/header-injection"))).text

        href = html.split('href="')[1].split('"')[0]
        assert "\r" in href or "\n" in href, "fixture no longer serves the payload"

        cleaned = normalize(href, base=server.base_url)
        assert "\r" not in cleaned.url
        assert "\n" not in cleaned.url
        assert "%0d" not in cleaned.url.lower()
        assert "%0a" not in cleaned.url.lower()
        # Inert: the smuggled text is now path, not a header line.
        assert cleaned.path.startswith("/ok")

    async def test_canonical_link_is_present_for_dedupe(self, server: RunningServer) -> None:
        import httpx

        async with httpx.AsyncClient() as client:
            html = (await client.get(server.url("/"))).text
        assert 'rel="canonical"' in html


class TestTrapsOverTheWire:
    """Follow the fixture's own links and confirm the budget bites.

    Discovery is exercised for real here rather than with synthetic URL lists,
    because the interesting failure is a link that normalises into a *new*
    pattern each time and quietly escapes the budget.
    """

    async def test_calendar_exhausts_one_budget_and_stops(self, server: RunningServer) -> None:
        import re

        import httpx

        guard = TrapGuard(SpiderConfig(per_pattern_budget=10))
        admitted = 0
        url = server.url("/calendar/2031/07")

        async with httpx.AsyncClient() as client:
            for _ in range(60):
                normalized = normalize(url)
                if not guard.check(normalized).ok:
                    break
                admitted += 1
                html = (await client.get(normalized.url)).text
                match = re.search(r'href="(/calendar/[^"]+)"', html)
                assert match
                url = server.url(match.group(1))

        assert admitted == 10, "an infinite calendar must not cost 60 fetches"

    async def test_session_trap_collapses_to_one_pattern(self, server: RunningServer) -> None:
        import re

        import httpx

        guard = TrapGuard(SpiderConfig(per_pattern_budget=5))
        admitted = 0
        url = server.url("/session/0000000000000000/page")

        async with httpx.AsyncClient() as client:
            for _ in range(40):
                normalized = normalize(url)
                if not guard.check(normalized).ok:
                    break
                admitted += 1
                html = (await client.get(normalized.url)).text
                match = re.search(r'href="(/session/[^"]+)"', html)
                assert match
                url = server.url(match.group(1))

        assert admitted == 5, "a fresh session id per link must not mint a fresh budget"

    async def test_deep_recursion_is_cut_by_path_depth(self, server: RunningServer) -> None:
        guard = TrapGuard(SpiderConfig(max_path_depth=8, max_repeated_segment=2))
        path = "/deep"
        verdicts = []
        for _ in range(12):
            path += "/a/b"
            verdicts.append(guard.check(normalize(server.url(path))))

        assert any(not v.ok for v in verdicts)
        first_denial = next(v for v in verdicts if not v.ok)
        assert first_denial.reason in (
            RejectReason.PATH_DEPTH,
            RejectReason.REPEATED_SEGMENT,
        )

    async def test_facet_explosion_is_capped_by_query_whitelist(
        self, server: RunningServer
    ) -> None:
        guard = TrapGuard(SpiderConfig(max_query_params=3, per_pattern_budget=1000))
        wide = server.url("/facet?color=a&size=b&brand=c&sort=d&page=e")
        assert guard.check(normalize(wide)).reason is RejectReason.QUERY_PARAMS

    async def test_whitelisted_facets_share_a_dedupe_key(self, server: RunningServer) -> None:
        """With a whitelist the combinations collapse to one frontier entry,
        while the fetch URL still carries the facets the user asked for."""
        whitelist = frozenset({"page"})
        a = normalize(server.url("/facet?color=a&size=b&page=1"), dedupe_whitelist=whitelist)
        b = normalize(server.url("/facet?color=c&size=d&page=1"), dedupe_whitelist=whitelist)
        assert a.dedupe_key == b.dedupe_key
        assert a.url != b.url


class TestScopeOverTheWire:
    async def test_offsite_link_is_out_of_scope(self, server: RunningServer) -> None:
        scope = DomainScope.from_spec(("example.com",))
        assert not scope.contains(normalize("https://cdn.evil-example.com/x").host)
        assert scope.contains(normalize("https://www.example.com/x").host)


class TestFixtureEndpointsExist:
    """Endpoints Phase 2 will need. Asserting they respond now keeps the
    fixture from rotting before the code that consumes it arrives."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/soft404", 200),
            ("/status/404", 404),
            ("/status/500", 500),
            ("/ratelimit", 429),
            ("/duplicate/1", 200),
        ],
    )
    async def test_endpoint_responds(self, server: RunningServer, path: str, expected: int) -> None:
        import httpx

        async with httpx.AsyncClient() as client:
            r = await client.get(server.url(path))
        assert r.status_code == expected

    async def test_ratelimit_carries_retry_after(self, server: RunningServer) -> None:
        import httpx

        async with httpx.AsyncClient() as client:
            r = await client.get(server.url("/ratelimit"))
        assert r.headers["retry-after"] == "2"

    async def test_soft404_returns_200_with_not_found_body(self, server: RunningServer) -> None:
        import httpx

        async with httpx.AsyncClient() as client:
            r = await client.get(server.url("/soft404"))
        assert r.status_code == 200
        assert "not found" in r.text.lower()

    async def test_huge_stream_never_ends(self, server: RunningServer) -> None:
        """Read a little and walk away - proves the endpoint streams without
        Content-Length, which is what makes a header-based limit useless."""
        import httpx

        async with (
            httpx.AsyncClient() as client,
            client.stream("GET", server.url("/huge")) as r,
        ):
            assert "content-length" not in r.headers
            read = 0
            async for chunk in r.aiter_bytes():
                read += len(chunk)
                if read > 32_768:
                    break
        assert read > 32_768
