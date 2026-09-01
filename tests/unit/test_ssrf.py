"""SSRF guard - docs/11_SECURITY_MODEL.md section 2.

Security code that has not been attacked is decorative. These tests attack it:
every blocked range, both IPv4-in-IPv6 spellings, and the rebinding race that
plain hostname validation loses.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

import pytest

from crwallm.policy.ssrf import (
    IPAddress,
    SsrfBlockedError,
    SsrfGuard,
    StaticResolver,
    classify_address,
)


def ip(s: str) -> IPAddress:
    return ipaddress.ip_address(s)


class TestClassifyAddress:
    @pytest.mark.parametrize(
        "addr",
        [
            "0.0.0.0",
            "10.0.0.1",
            "100.100.100.200",  # Alibaba metadata, inside CGNAT
            "127.0.0.1",
            "127.1.2.3",
            "169.254.169.254",  # AWS / GCP / Azure metadata
            "172.16.0.1",
            "172.31.255.255",
            "192.0.0.1",
            "192.0.2.1",
            "192.88.99.1",
            "192.168.1.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
        ],
    )
    def test_blocked_ipv4(self, addr: str) -> None:
        assert classify_address(ip(addr)) is not None

    @pytest.mark.parametrize(
        "addr",
        [
            "::",
            "::1",
            "fc00::1",
            "fd12:3456::1",
            "fe80::1",
            "ff02::1",
            "2001:db8::1",
            "64:ff9b::1",
            "100::1",
        ],
    )
    def test_blocked_ipv6(self, addr: str) -> None:
        assert classify_address(ip(addr)) is not None

    @pytest.mark.parametrize(
        "addr",
        ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "2001:4860:4860::8888"],
    )
    def test_public_addresses_pass(self, addr: str) -> None:
        assert classify_address(ip(addr)) is None

    @pytest.mark.parametrize(
        "addr",
        [
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:169.254.169.254",  # IPv4-mapped metadata
            "::ffff:10.0.0.1",
            "::ffff:192.168.1.1",
        ],
    )
    def test_ipv4_mapped_is_unwrapped(self, addr: str) -> None:
        """The classic bypass: loopback wearing an IPv6 costume."""
        assert classify_address(ip(addr)) is not None

    def test_sixtofour_is_unwrapped(self) -> None:
        # 2002:0a00:0001:: encodes 10.0.0.1
        assert classify_address(ip("2002:a00:1::")) is not None

    def test_ipv4_compatible_is_unwrapped(self) -> None:
        # Deprecated ::a.b.c.d form
        assert classify_address(ip("::127.0.0.1")) is not None


class TestGuard:
    @staticmethod
    def guard(table: dict[str, Sequence[str]]) -> SsrfGuard:
        return SsrfGuard(StaticResolver(table))  # type: ignore[arg-type]

    async def test_public_host_is_pinned(self) -> None:
        g = self.guard({"example.com": ["93.184.216.34"]})
        t = await g.check("https://example.com/page")
        assert t.host == "example.com"
        assert str(t.ip) == "93.184.216.34"
        assert t.port == 443
        assert t.is_tls

    async def test_host_survives_for_sni_and_host_header(self) -> None:
        """Pinning must not lose the hostname - virtual hosting and
        certificate validation both need it."""
        g = self.guard({"example.com": ["93.184.216.34"]})
        t = await g.check("https://example.com/")
        assert t.connect_host == "93.184.216.34"
        assert t.host == "example.com"

    async def test_internal_resolution_is_blocked(self) -> None:
        g = self.guard({"evil.test": ["127.0.0.1"]})
        with pytest.raises(SsrfBlockedError, match=r"loopback|blocked range"):
            await g.check("http://evil.test/")

    async def test_metadata_endpoint_is_blocked(self) -> None:
        g = self.guard({"meta.test": ["169.254.169.254"]})
        with pytest.raises(SsrfBlockedError):
            await g.check("http://meta.test/latest/meta-data/")

    async def test_ip_literal_skips_dns(self) -> None:
        g = self.guard({})
        with pytest.raises(SsrfBlockedError):
            await g.check("http://127.0.0.1:8000/")

    async def test_public_ip_literal_is_allowed(self) -> None:
        g = self.guard({})
        t = await g.check("http://1.1.1.1/")
        assert str(t.ip) == "1.1.1.1"

    async def test_ipv6_literal_is_bracketed_for_connect(self) -> None:
        g = self.guard({})
        t = await g.check("http://[2606:4700:4700::1111]/")
        assert t.connect_host == "[2606:4700:4700::1111]"

    async def test_any_internal_answer_blocks_the_host(self) -> None:
        """A host that resolves to public *and* internal addresses is refused.

        Trying the next record would let an attacker pair a decoy public A
        record with the address they actually want reached.
        """
        g = self.guard({"mixed.test": ["127.0.0.1", "93.184.216.34"]})
        with pytest.raises(SsrfBlockedError):
            await g.check("http://mixed.test/")

    @pytest.mark.parametrize(
        "url", ["file:///etc/passwd", "ftp://a.com/", "gopher://a.com/", "javascript:x"]
    )
    async def test_non_http_scheme_is_blocked(self, url: str) -> None:
        with pytest.raises(SsrfBlockedError):
            await self.guard({}).check(url)

    async def test_unresolvable_host_is_blocked(self) -> None:
        with pytest.raises(SsrfBlockedError, match="no static answer"):
            await self.guard({}).check("http://nowhere.test/")

    async def test_default_ports_are_filled_in(self) -> None:
        g = self.guard({"a.test": ["1.1.1.1"]})
        assert (await g.check("http://a.test/")).port == 80
        assert (await g.check("https://a.test/")).port == 443

    async def test_explicit_port_is_kept(self) -> None:
        g = self.guard({"a.test": ["1.1.1.1"]})
        assert (await g.check("http://a.test:8080/")).port == 8080


class TestDnsRebinding:
    """The reason pinning exists.

    Validating a hostname and then connecting by hostname loses this race:
    the resolver runs a second time and can answer differently.
    """

    class RebindingResolver:
        """Public on the first lookup, loopback on every one after."""

        def __init__(self) -> None:
            self.calls = 0

        async def resolve(self, host: str) -> Sequence[IPAddress]:
            self.calls += 1
            if self.calls == 1:
                return [ip("93.184.216.34")]
            return [ip("127.0.0.1")]

    async def test_guard_resolves_exactly_once(self) -> None:
        resolver = self.RebindingResolver()
        guard = SsrfGuard(resolver)  # type: ignore[arg-type]
        target = await guard.check("http://rebind.test/")

        assert resolver.calls == 1, "a second lookup is a second chance to rebind"
        assert str(target.ip) == "93.184.216.34"

    async def test_pinned_address_is_what_the_fetcher_connects_to(self) -> None:
        """The contract the fetcher relies on: connect to ``target.ip``, never
        re-resolve ``target.host``."""
        resolver = self.RebindingResolver()
        guard = SsrfGuard(resolver)  # type: ignore[arg-type]
        target = await guard.check("http://rebind.test/")

        assert classify_address(target.ip) is None
        assert target.connect_host == "93.184.216.34"


class TestAllowNetworks:
    """Test-only escape hatch, exercised here so its blast radius is documented."""

    async def test_loopback_can_be_allowed_explicitly(self) -> None:
        g = SsrfGuard(
            StaticResolver({"local.test": ["127.0.0.1"]}),  # type: ignore[arg-type]
            allow_networks=[ipaddress.ip_network("127.0.0.0/8")],
        )
        assert str((await g.check("http://local.test/")).ip) == "127.0.0.1"

    async def test_allowlist_does_not_leak_to_other_ranges(self) -> None:
        g = SsrfGuard(
            StaticResolver({"meta.test": ["169.254.169.254"]}),  # type: ignore[arg-type]
            allow_networks=[ipaddress.ip_network("127.0.0.0/8")],
        )
        with pytest.raises(SsrfBlockedError):
            await g.check("http://meta.test/")


class TestCachingResolver:
    async def test_repeated_lookups_hit_the_cache(self) -> None:
        from crwallm.policy.ssrf import CachingResolver

        class Counting:
            def __init__(self) -> None:
                self.calls = 0

            async def resolve(self, host: str) -> Sequence[IPAddress]:
                self.calls += 1
                return [ip("1.1.1.1")]

        inner = Counting()
        cache = CachingResolver(inner, ttl_s=60)  # type: ignore[arg-type]
        for _ in range(5):
            await cache.resolve("a.test")
        assert inner.calls == 1

    async def test_concurrent_lookups_collapse_to_one(self) -> None:
        import asyncio

        from crwallm.policy.ssrf import CachingResolver

        class Slow:
            def __init__(self) -> None:
                self.calls = 0

            async def resolve(self, host: str) -> Sequence[IPAddress]:
                self.calls += 1
                await asyncio.sleep(0.01)
                return [ip("1.1.1.1")]

        inner = Slow()
        cache = CachingResolver(inner)  # type: ignore[arg-type]
        await asyncio.gather(*(cache.resolve("a.test") for _ in range(20)))
        assert inner.calls == 1, "a burst of links to one host is one DNS query"
