"""DNS-pinning transport.

This is where ``SsrfGuard`` stops being a predicate and becomes a control. The
guard resolves a hostname once, validates the address and hands back a
``PinnedTarget``; without this module the fetcher would hand the *hostname* to
httpx, httpx would resolve it again, and every check the guard performed would
apply to an address we never connected to (docs/11_SECURITY_MODEL.md section 2).

**Where the substitution happens, and why there.** Two placements work on
paper:

*Rewrite the URL to the IP.* Rejected. httpcore pools connections by origin -
``(scheme, host, port)`` - and SNI is not part of that key. Two hostnames that
pin to the same address (shared hosting, any CDN) would collapse into one pool
entry, and the second hostname's requests would ride a TLS session validated
for the first. Correct SSRF behaviour, broken certificate verification.

*Substitute inside the network backend.* Taken. The URL keeps the real
hostname, so the pool keys on it, the ``Host`` header is right, SNI is right
and the certificate is checked against the name we asked for. Only the TCP
connect sees the pinned address.

**Fail closed.** ``connect_tcp`` refuses any host that is not in the registry.
Bypassing the guard therefore requires deleting code rather than forgetting a
call - the failure mode is a refused connection, not a silent internal fetch.
"""

from __future__ import annotations

import contextlib
import typing
from dataclasses import dataclass, field

import httpcore
import httpx
from httpcore import AsyncNetworkBackend, AsyncNetworkStream
from httpcore._backends.anyio import AnyIOBackend

from crwallm.policy.ssrf import IPAddress

__all__ = ["PinRegistry", "PinnedBackend", "PinnedTransport", "UnpinnedHostError"]


class UnpinnedHostError(Exception):
    """A connection was attempted for a host the guard never approved."""


@dataclass(slots=True)
class PinRegistry:
    """Approved (host, port) -> address.

    Populated by the fetcher immediately after ``SsrfGuard.check`` and read by
    the backend at connect time. Keyed by host and port rather than host alone
    so a crawl can hold different pins for ``example.com:80`` and
    ``example.com:8443`` without them fighting.
    """

    _pins: dict[tuple[str, int], IPAddress] = field(default_factory=dict)

    def pin(self, host: str, port: int, address: IPAddress) -> None:
        self._pins[(host.lower(), port)] = address

    def lookup(self, host: str, port: int) -> IPAddress | None:
        return self._pins.get((host.lower(), port))

    def release(self, host: str, port: int) -> None:
        self._pins.pop((host.lower(), port), None)

    def clear(self) -> None:
        self._pins.clear()

    def __len__(self) -> int:
        return len(self._pins)


class PinnedBackend(AsyncNetworkBackend):
    """Connects to the pinned address instead of resolving the hostname."""

    def __init__(
        self,
        pins: PinRegistry,
        inner: AsyncNetworkBackend | None = None,
    ) -> None:
        self._pins = pins
        self._inner = inner if inner is not None else AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> AsyncNetworkStream:
        address = self._pins.lookup(host, port)
        if address is None:
            # Fail closed. Reaching here means a request was built without
            # going through the guard.
            raise httpcore.ConnectError(
                f"refusing to connect to unpinned host {host!r}:{port} - "
                "every fetch must pass SsrfGuard.check first"
            )
        return await self._inner.connect_tcp(
            str(address),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not a crawl target")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncBaseTransport):
    """httpx transport over a pool wired to :class:`PinnedBackend`.

    httpx's own ``AsyncHTTPTransport`` builds its pool internally and offers no
    way to supply a network backend, so the pool is constructed here. The body
    mirrors httpx's transport: translate the request, hand it to httpcore,
    translate the response back.
    """

    def __init__(
        self,
        pins: PinRegistry,
        *,
        http2: bool = True,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        keepalive_expiry: float = 30.0,
        retries: int = 0,
        verify: bool = True,
    ) -> None:
        self._pins = pins
        ssl_context = httpx.create_ssl_context(verify=verify)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            network_backend=PinnedBackend(pins),
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
            http1=True,
            http2=http2,
            # Retries would re-enter connect_tcp, which is harmless because the
            # pin is still in force - but a crawler would rather see the error
            # and make its own decision about backing off.
            retries=retries,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)

        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _as_httpx_errors():
            resp = await self._pool.handle_async_request(req)
        assert isinstance(resp.stream, typing.AsyncIterable)

        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_ResponseStream(resp.stream),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class _ResponseStream(httpx.AsyncByteStream):
    """httpcore's byte stream, wearing httpx's interface.

    httpx keeps its own wrapper private, and the body has to stay lazy: the
    streaming byte limit in ``fetching.http`` works by abandoning this iterator
    part-way through, which only stops the transfer if nothing has buffered it.
    """

    def __init__(self, stream: typing.AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        with _as_httpx_errors():
            async for chunk in self._stream:
                yield chunk

    async def aclose(self) -> None:
        aclose = getattr(self._stream, "aclose", None)
        if aclose is not None:
            await aclose()


_ERROR_MAP: dict[type[Exception], type[httpx.HTTPError]] = {
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.NetworkError: httpx.NetworkError,
}


@contextlib.contextmanager
def _as_httpx_errors() -> typing.Iterator[None]:
    """Translate httpcore exceptions to their httpx equivalents.

    httpx does this in a private helper. Reimplemented rather than imported so
    an httpx point release cannot quietly turn every network failure in the
    crawler into an unclassified ``INTERNAL`` error.
    """
    try:
        yield
    except Exception as exc:
        mapped = _ERROR_MAP.get(type(exc))
        if mapped is None:
            raise
        raise mapped(str(exc)) from exc
