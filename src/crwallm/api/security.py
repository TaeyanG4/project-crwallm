"""Local API hardening.

An unauthenticated API bound to localhost is reachable by any web page the
user visits: a malicious site's JS can POST to ``http://127.0.0.1:8000`` and
drive the crawler. DNS rebinding defeats a naive CORS-only defence.

Three cheap layers, all required (docs/11_SECURITY_MODEL.md §1):

1. bind to 127.0.0.1 only                     -> ``Settings.api_host``
2. Host header allowlist                      -> ``HostHeaderMiddleware``
3. custom header on mutating routes           -> ``require_token``

(3) matters more than it looks: requiring a custom header makes the request
non-"simple", so the browser must preflight it, and CORS blocks the preflight.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from crwallm.config import Settings

TOKEN_HEADER = "X-CRWALLM-Token"  # noqa: S105 - header name, not a secret


class HostHeaderMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Host header is not in the allowlist.

    Blocks DNS rebinding: an attacker-controlled name that resolves to
    127.0.0.1 still arrives with its own Host header.
    """

    def __init__(self, app: ASGIApp, allowed: list[str]) -> None:
        super().__init__(app)
        self._allowed = {h.lower() for h in allowed}

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        host = (request.headers.get("host") or "").split(":", 1)[0].lower()
        if host and host not in self._allowed:
            return JSONResponse(
                status_code=status.HTTP_421_MISDIRECTED_REQUEST,
                content={"detail": f"Host {host!r} not allowed"},
            )
        return await call_next(request)


def make_token_dependency(settings: Settings) -> Callable[[str | None], None]:
    """Build the dependency guarding mutating endpoints."""

    def require_token(
        token: str | None = Header(default=None, alias=TOKEN_HEADER),
    ) -> None:
        expected = settings.api_token
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"{TOKEN_HEADER} is not configured. Set CRWALLM_API_TOKEN — see .env.example"
                ),
            )
        # Constant-time compare; the token is short but there is no reason
        # to leak its prefix through timing.
        import hmac

        if token is None or not hmac.compare_digest(token, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"missing or invalid {TOKEN_HEADER}",
            )

    return require_token
