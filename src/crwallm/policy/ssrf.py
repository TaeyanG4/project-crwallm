"""SSRF guard with DNS pinning.

Crawl targets are attacker-influenced: they arrive from links on pages we just
crawled, from redirects, and from model output. A link to
``http://169.254.169.254/latest/meta-data/`` is a credential exfiltration
attempt dressed as a URL.

Five layers, of which this module owns the first three
(docs/11_SECURITY_MODEL.md section 2)::

    1. scheme allowlist               http, https only
    2. resolve -> validate the IP     denylist below
    3. connect to the validated IP    <- pinning
    4. re-validate every redirect hop (fetcher, Phase 2)
    5. browser frame + subresources   (Phase 7)

**Why pinning.** Validating the hostname is not enough, because the resolver
runs again at connect time::

    t0  resolve evil.com -> 1.2.3.4      public, passes
    t1  validate OK
    t2  connect -> resolver runs again -> 127.0.0.1   (DNS rebinding, TTL 0)

We validated one host and connected to another. Resolving once, checking the
address, and then connecting to *that address* - carrying the original
hostname in the Host header and the TLS SNI - makes the checked target and the
connected target the same object. The DNS cache that falls out of this is a
performance win too (docs/12_PERFORMANCE.md).

**Why the resolver is injected.** Rebinding cannot be tested against a real
resolver without running a DNS server. With ``Resolver`` as a protocol, a test
substitutes one that answers with a public address first and a loopback
address second: if pinning works, the second answer is never requested.
Testability drove this design.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from crwallm.policy.url import NormalizedUrl, UrlNormalizationError, normalize

__all__ = [
    "DENIED_NETWORKS",
    "CachingResolver",
    "PinnedTarget",
    "Resolver",
    "SsrfBlockedError",
    "SsrfGuard",
    "StaticResolver",
    "SystemResolver",
    "classify_address",
]

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


DENIED_NETWORKS: tuple[IPNetwork, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        # -- IPv4 ------------------------------------------------------------
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # CGNAT - also Alibaba metadata at 100.100.100.200
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local - AWS/GCP/Azure metadata at .169.254
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # 6to4 relay anycast
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "255.255.255.255/32",  # broadcast
        # -- IPv6 ------------------------------------------------------------
        "::/128",  # unspecified
        "::1/128",  # loopback
        "64:ff9b::/96",  # NAT64
        "100::/64",  # discard-only
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
)
"""Explicit list rather than ``ipaddress`` flags alone.

``is_private`` misses several of these depending on the Python version, and a
denylist that silently narrows across an interpreter upgrade is the kind of
regression nobody notices. The flags are still consulted as a backstop in
``classify_address``.
"""


class SsrfBlockedError(Exception):
    """A target was refused. ``reason`` is safe to surface; it never contains
    anything the caller did not already supply."""

    def __init__(self, reason: str, *, url: str | None = None) -> None:
        self.reason = reason
        self.url = url
        super().__init__(f"{reason}" + (f" ({url})" if url else ""))


def _unwrap(addr: IPAddress) -> IPAddress:
    """Resolve an IPv6 address down to the IPv4 address it actually denotes.

    ``::ffff:127.0.0.1`` is loopback wearing an IPv6 costume, and checking it
    against the IPv6 networks alone lets it through. A classic bypass.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        if addr.sixtofour is not None:
            return addr.sixtofour
        # ::a.b.c.d - deprecated IPv4-compatible form
        if int(addr) >> 32 == 0 and int(addr) != 0 and int(addr) != 1:
            return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return addr


def classify_address(addr: IPAddress) -> str | None:
    """Return a rejection reason, or ``None`` when the address is routable.

    Checks the unwrapped form so IPv4-in-IPv6 spellings cannot slip past.
    """
    target = _unwrap(addr)

    for net in DENIED_NETWORKS:
        if target.version == net.version and target in net:
            return f"address {target} is in blocked range {net}"

    # Backstop: anything the explicit list missed but the stdlib recognises.
    if target.is_loopback:
        return f"address {target} is loopback"
    if target.is_link_local:
        return f"address {target} is link-local"
    if target.is_private:
        return f"address {target} is private"
    if target.is_multicast:
        return f"address {target} is multicast"
    if target.is_reserved:
        return f"address {target} is reserved"
    if target.is_unspecified:
        return f"address {target} is unspecified"
    return None


# --------------------------------------------------------------- resolvers


class Resolver(Protocol):
    """Hostname to addresses.

    Injected rather than imported so tests can model DNS rebinding.
    """

    async def resolve(self, host: str) -> Sequence[IPAddress]: ...


class SystemResolver:
    """``getaddrinfo`` on the event loop's executor."""

    async def resolve(self, host: str) -> Sequence[IPAddress]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                host, None, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        except socket.gaierror as exc:
            raise SsrfBlockedError(f"DNS resolution failed: {exc}") from exc
        out: list[IPAddress] = []
        for info in infos:
            sockaddr = info[4]
            try:
                out.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:  # pragma: no cover - getaddrinfo shouldn't
                continue
        if not out:
            raise SsrfBlockedError("DNS returned no addresses")
        return out


class StaticResolver:
    """Fixed answers. For tests and for pinning a known host."""

    def __init__(self, table: dict[str, Sequence[str | IPAddress]]) -> None:
        self._table = {
            host.lower(): [ipaddress.ip_address(str(a)) for a in addrs]
            for host, addrs in table.items()
        }

    async def resolve(self, host: str) -> Sequence[IPAddress]:
        try:
            return self._table[host.lower()]
        except KeyError:
            raise SsrfBlockedError(f"no static answer for {host}") from None


@dataclass(slots=True)
class _CacheEntry:
    addresses: Sequence[IPAddress]
    expires_at: float


class CachingResolver:
    """TTL cache in front of another resolver.

    Cuts 20-100ms off every new connection. Because the guard pins whichever
    address it validated, a stale entry cannot widen what we are willing to
    reach - it can only send us to an address that already passed.
    """

    def __init__(
        self,
        inner: Resolver,
        *,
        ttl_s: float = 300.0,
        max_entries: int = 10_000,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_s
        self._max = max_entries
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(self, host: str) -> Sequence[IPAddress]:
        key = host.lower()
        now = time.monotonic()

        entry = self._cache.get(key)
        if entry is not None and entry.expires_at > now:
            return entry.addresses

        # One in-flight lookup per host; a burst of links to the same site
        # should not become a burst of identical DNS queries.
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._cache.get(key)
            if entry is not None and entry.expires_at > time.monotonic():
                return entry.addresses
            addresses = await self._inner.resolve(host)
            if len(self._cache) >= self._max:
                self._evict_expired()
            self._cache[key] = _CacheEntry(addresses, time.monotonic() + self._ttl)
            self._locks.pop(key, None)
            return addresses

    def _evict_expired(self) -> None:
        now = time.monotonic()
        stale = [k for k, v in self._cache.items() if v.expires_at <= now]
        for k in stale:
            del self._cache[k]
        if len(self._cache) >= self._max:  # still full - drop the oldest half
            ordered = sorted(self._cache.items(), key=lambda kv: kv[1].expires_at)
            for k, _ in ordered[: len(ordered) // 2]:
                del self._cache[k]

    def clear(self) -> None:
        self._cache.clear()


# ------------------------------------------------------------------- guard


@dataclass(frozen=True, slots=True)
class PinnedTarget:
    """A validated destination.

    Connect to ``ip``. Send ``host`` in the Host header and as the TLS SNI, so
    virtual hosting and certificate validation still work.
    """

    scheme: str
    host: str
    port: int
    ip: IPAddress
    url: str

    @property
    def is_tls(self) -> bool:
        return self.scheme == "https"

    @property
    def connect_host(self) -> str:
        return f"[{self.ip}]" if self.ip.version == 6 else str(self.ip)


class SsrfGuard:
    """Validates a URL and returns something safe to connect to."""

    def __init__(
        self,
        resolver: Resolver | None = None,
        *,
        allow_networks: Sequence[IPNetwork] = (),
    ) -> None:
        self._resolver = resolver if resolver is not None else CachingResolver(SystemResolver())
        self._allowed = tuple(allow_networks)
        """Escape hatch for tests that must reach a loopback fixture server.

        Never populate this from user input or config: an allowlist entry
        disables the guard for that range, which is exactly what an attacker
        wants."""

    def _explicitly_allowed(self, addr: IPAddress) -> bool:
        target = _unwrap(addr)
        return any(target.version == net.version and target in net for net in self._allowed)

    async def check(self, url: str | NormalizedUrl) -> PinnedTarget:
        """Resolve, validate and pin. Raises ``SsrfBlockedError`` on refusal."""
        if isinstance(url, str):
            try:
                parsed = normalize(url)
            except UrlNormalizationError as exc:
                raise SsrfBlockedError(str(exc), url=url) from exc
        else:
            parsed = url

        if parsed.scheme not in ("http", "https"):  # pragma: no cover - normalize guards
            raise SsrfBlockedError(f"unsupported scheme {parsed.scheme!r}", url=parsed.url)

        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # An IP literal skips DNS entirely; there is nothing to rebind.
        literal = _as_literal(parsed.host)
        addresses: Sequence[IPAddress]
        if literal is not None:
            addresses = [literal]
        else:
            addresses = await self._resolver.resolve(parsed.host)
            if not addresses:
                raise SsrfBlockedError("DNS returned no addresses", url=parsed.url)

        # Every answer must pass, not merely one of them.
        #
        # Pinning already makes the permissive reading safe on this path - we
        # connect to the address we checked. But a mixed answer (one decoy
        # public record beside the address the attacker actually wants) is not
        # something a real site produces, and the strict reading also covers
        # the paths that cannot pin: the browser fetcher (Phase 7) hands the
        # hostname to Chromium and gets no say in which record it picks.
        for addr in addresses:
            if self._explicitly_allowed(addr):
                continue
            reason = classify_address(addr)
            if reason is not None:
                raise SsrfBlockedError(reason, url=parsed.url)

        return PinnedTarget(parsed.scheme, parsed.host, port, addresses[0], parsed.url)


def _as_literal(host: str) -> IPAddress | None:
    stripped = host.strip("[]")
    try:
        return ipaddress.ip_address(stripped)
    except ValueError:
        return None
