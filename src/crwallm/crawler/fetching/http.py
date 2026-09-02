"""The HTTP fetcher.

Three things happen here that cannot happen anywhere else, because each of
them is a place where handing control to the library would quietly undo a
guarantee made elsewhere:

**Pinning is enforced.** ``SsrfGuard`` runs per hop and the approved address
goes into the ``PinRegistry`` that the transport connects through. Give httpx
a hostname and it resolves it again; see ``fetching.pinning``.

**Redirects are followed by hand.** ``follow_redirects=True`` would let httpx
walk hops two through five on its own, and the guard would never see them.
A 302 to ``169.254.169.254`` is the single most common way SSRF ships, and it
is invisible when the library owns the redirect loop.

**The size limit is enforced while reading.** Checking ``Content-Length`` stops
nothing: the fixture serves an endless stream with no length header, another
that claims ten bytes and never stops, and a gzip bomb that is small on the
wire. So bytes are counted as they arrive, after decompression, and the
transfer is abandoned the moment the budget is gone.
"""

from __future__ import annotations

import email.utils
import ssl
import time
from dataclasses import dataclass

import httpx

from crwallm.crawler.contracts import (
    FetchFailure,
    FetchOutcome,
    FetchRequest,
    FetchResponse,
)
from crwallm.crawler.fetching.pinning import PinnedTransport, PinRegistry
from crwallm.policy.ssrf import SsrfBlockedError, SsrfGuard
from crwallm.policy.url import NormalizedUrl, UrlNormalizationError, normalize
from crwallm.schemas.types import ErrorKind, FetchMode

__all__ = ["DEFAULT_USER_AGENT", "SafeHttpFetcher"]

DEFAULT_USER_AGENT = "crwallm/0.1 (+https://github.com/TaeyanG4/project-crwallm)"
"""Identifiable on purpose.

Politeness rules are relaxed for this tool (docs/17_NON_GOALS.md), but
impersonating a browser is a different thing from skipping robots.txt, and
docs/17 rules it out. An operator who wants to complain should be able to work
out who to complain to.
"""

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def supported_encodings() -> str:
    """``Accept-Encoding`` built from what can actually be decoded.

    Advertising an encoding the client cannot decode does not fail - it
    succeeds, and stores compressed bytes as if they were HTML. Every record
    from that page is then quietly wrong, and nothing in the crawl says so.
    This happened during Phase 2 with brotli: httpx only decodes it when
    ``brotli`` is installed, most CDN-fronted sites prefer it, and the result
    was a 200 with a body of noise.

    Asking httpx what it supports rather than hard-coding a list means a
    missing dependency degrades to gzip instead of corrupting data.
    """
    from httpx._decoders import SUPPORTED_DECODERS

    preferred = [e for e in ("br", "zstd", "gzip", "deflate") if e in SUPPORTED_DECODERS]
    return ", ".join(preferred) or "identity"


@dataclass(frozen=True, slots=True)
class _Hop:
    """One step of a redirect chain."""

    url: NormalizedUrl
    status: int


class SafeHttpFetcher:
    """``Fetcher`` over httpx, with the guard wired into every hop."""

    def __init__(
        self,
        guard: SsrfGuard,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        http2: bool = True,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        verify: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._guard = guard
        self._pins = PinRegistry()
        self._transport = PinnedTransport(
            self._pins,
            http2=http2,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            verify=verify,
        )
        headers = {
            "User-Agent": user_agent,
            "Accept-Encoding": supported_encodings(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko,en;q=0.9",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.AsyncClient(
            transport=self._transport,
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(15.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        self._pins.clear()

    async def fetch(self, request: FetchRequest) -> FetchOutcome:
        started = time.perf_counter()
        current = request.url
        chain: list[_Hop] = []
        # Loop detection by identity, not just by hop count. A two-URL cycle
        # would otherwise burn the whole redirect budget before stopping.
        seen: set[str] = {current.dedupe_key}

        while True:
            try:
                target = await self._guard.check(current)
            except SsrfBlockedError as exc:
                return FetchFailure(
                    url=current,
                    error_kind=ErrorKind.SSRF_REJECT,
                    message=exc.reason,
                )

            self._pins.pin(target.host, target.port, target.ip)

            try:
                outcome = await self._stream_once(current, request, started, redirects=len(chain))
            except Exception as exc:  # classified below, never swallowed
                return _classify_transport_error(current, exc)

            if isinstance(outcome, FetchFailure):
                return outcome

            if outcome.status not in REDIRECT_STATUSES:
                return outcome

            location = outcome.headers.get("location")
            if not location:
                # A redirect status with nowhere to go. Treat the body as the
                # answer rather than inventing one.
                return outcome

            if len(chain) >= request.max_redirects:
                return FetchFailure(
                    url=current,
                    error_kind=ErrorKind.REDIRECT_MAX,
                    message=f"more than {request.max_redirects} redirects",
                    status=outcome.status,
                )

            try:
                nxt = normalize(location, base=current.url)
            except UrlNormalizationError as exc:
                # Covers the scheme downgrade case: a 302 to file:///etc/passwd
                # dies here rather than at the socket.
                return FetchFailure(
                    url=current,
                    error_kind=ErrorKind.SSRF_REJECT,
                    message=f"redirect target rejected: {exc}",
                    status=outcome.status,
                )

            if nxt.dedupe_key in seen:
                return FetchFailure(
                    url=current,
                    error_kind=ErrorKind.REDIRECT_LOOP,
                    message=f"redirect cycle back to {nxt.url}",
                    status=outcome.status,
                )

            chain.append(_Hop(current, outcome.status))
            seen.add(nxt.dedupe_key)
            current = nxt

    async def _stream_once(
        self,
        url: NormalizedUrl,
        request: FetchRequest,
        started: float,
        *,
        redirects: int,
    ) -> FetchOutcome:
        timeout = httpx.Timeout(
            connect=request.timeout_s,
            read=request.timeout_s,
            write=request.timeout_s,
            pool=request.timeout_s,
        )
        async with self._client.stream("GET", url.url, timeout=timeout) as response:
            body = bytearray()
            over_limit = False
            # aiter_bytes yields *decoded* bytes, so a gzip bomb is measured at
            # its real size rather than its compressed one.
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > request.byte_limit:
                    over_limit = True
                    break

            if over_limit:
                # Leaving the context manager closes the connection, which is
                # what actually stops an endless transfer.
                return FetchFailure(
                    url=url,
                    error_kind=ErrorKind.SIZE_EXCEEDED,
                    message=f"body exceeded {request.byte_limit} bytes",
                    status=response.status_code,
                )

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            headers = {k.lower(): v for k, v in response.headers.items()}

            if response.status_code in REDIRECT_STATUSES:
                return FetchResponse(
                    url=url,
                    status=response.status_code,
                    headers=headers,
                    body=bytes(body),
                    elapsed_ms=elapsed_ms,
                    fetch_mode=FetchMode.HTTP,
                    redirects=redirects,
                )

            failure = _classify_status(url, response.status_code, headers)
            if failure is not None:
                return failure

            return FetchResponse(
                url=url,
                status=response.status_code,
                headers=headers,
                body=bytes(body),
                elapsed_ms=elapsed_ms,
                fetch_mode=FetchMode.HTTP,
                redirects=redirects,
                final_url=url.url if redirects else None,
            )


def _classify_status(
    url: NormalizedUrl, status: int, headers: dict[str, str]
) -> FetchFailure | None:
    """Map a response status onto the error taxonomy.

    The point of separating 403 and 429 from the rest of 4xx is operational:
    "400 pages failed" is not actionable, "380 of them were blocked_429" says
    to lower concurrency. docs/09_JOB_ARCHITECTURE.md
    """
    if status < 400:
        return None

    retry_after = _parse_retry_after(headers.get("retry-after"))

    if status == 429:
        return FetchFailure(
            url=url,
            error_kind=ErrorKind.BLOCKED_429,
            message="rate limited",
            retryable=True,
            status=status,
            retry_after_s=retry_after,
        )
    if status == 403:
        return FetchFailure(
            url=url,
            error_kind=ErrorKind.BLOCKED_403,
            message="forbidden",
            retryable=False,
            status=status,
            retry_after_s=retry_after,
        )
    if status >= 500:
        return FetchFailure(
            url=url,
            error_kind=ErrorKind.HTTP_5XX,
            message=f"server error {status}",
            retryable=True,
            status=status,
            retry_after_s=retry_after,
        )
    return FetchFailure(
        url=url,
        error_kind=ErrorKind.HTTP_4XX,
        message=f"client error {status}",
        retryable=False,
        status=status,
    )


def _classify_transport_error(url: NormalizedUrl, exc: Exception) -> FetchFailure:
    """Every network failure gets a name.

    An unclassified failure shows up as ``internal``, which is a signal that
    this mapping needs a new case rather than a reason to shrug.
    """
    match exc:
        case httpx.ConnectTimeout():
            kind, retryable = ErrorKind.CONN_TIMEOUT, True
        case httpx.ReadTimeout() | httpx.WriteTimeout() | httpx.PoolTimeout():
            kind, retryable = ErrorKind.READ_TIMEOUT, True
        case httpx.ConnectError():
            # The pinned transport refuses unpinned hosts with a ConnectError;
            # anything else here is a real refusal or an unreachable address.
            kind, retryable = (
                (ErrorKind.SSRF_REJECT, False)
                if "unpinned host" in str(exc)
                else (ErrorKind.CONN_REFUSED, True)
            )
        case httpx.ProtocolError() | httpx.RemoteProtocolError():
            kind, retryable = ErrorKind.PARSE_FAIL, False
        case httpx.TransportError() if isinstance(exc.__cause__, ssl.SSLError):
            kind, retryable = ErrorKind.TLS_ERROR, False
        case httpx.TransportError():
            kind, retryable = ErrorKind.CONN_REFUSED, True
        case _:
            kind, retryable = ErrorKind.INTERNAL, False

    return FetchFailure(
        url=url,
        error_kind=kind,
        message=f"{type(exc).__name__}: {exc}",
        retryable=retryable,
    )


def _parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` in either spelling: delta-seconds or an HTTP date.

    Feeds the adaptive concurrency controller - a host that tells us how long
    to wait is worth listening to, since the alternative is being blocked.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed.timestamp() - time.time())
