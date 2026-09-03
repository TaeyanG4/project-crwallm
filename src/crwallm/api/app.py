"""FastAPI application factory."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from crwallm import __version__
from crwallm.api.routers import chat, health, jobs, recipes, ui
from crwallm.api.security import TOKEN_HEADER, HostHeaderMiddleware
from crwallm.config import Settings, get_settings
from crwallm.db.session import dispose_engine

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    log.info("crwallm %s starting (env=%s)", __version__, settings.env)
    yield
    await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="CRWALLM",
        version=__version__,
        summary="Local AI crawler",
        lifespan=lifespan,
        docs_url="/docs" if settings.is_dev else None,
        redoc_url=None,
    )
    app.state.settings = settings

    # Order matters: the Host check runs before CORS so a rebinding attempt is
    # rejected outright rather than being handed a CORS decision.
    app.add_middleware(HostHeaderMiddleware, allowed=settings.allowed_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", TOKEN_HEADER],
    )

    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(recipes.router)
    app.include_router(chat.router)
    app.include_router(ui.router)
    _serve_page(app, settings)
    return app


def _serve_page(app: FastAPI, settings: Settings) -> None:
    """Serve the window's page from this process, on this port.

    The same three files the desktop window loads from disk. Running them here
    is what collapses two local servers into one: there is no Node, no second
    port, and no proxy in front of the API - the page and the API it calls are
    the same origin, so the browser needs no CORS decision at all.

    **The token is written into the page.** The page has to send
    ``X-CRWALLM-Token`` on its calls, which means it has to know it, which
    means it is in the HTML. That is the same trade the Next.js proxy made and
    it holds for the same reason: a page on another origin cannot read this
    one's body, so it cannot learn the token - and the Host allowlist above
    still blocks the rebinding attack that would let it try.
    """
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from crwallm import ui as ui_files

    root = ui_files.root()
    index = root / ui_files.INDEX
    if not index.exists():  # pragma: no cover - a broken install, not a state
        log.warning("UI files missing at %s; serving the API only", root)
        return

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    async def page() -> HTMLResponse:
        html = index.read_text(encoding="utf-8")
        marker = "<head>"
        token = json.dumps(settings.api_token or "")
        injected = f"{marker}\n<script>window.CRWALLM_TOKEN = {token};</script>"
        return HTMLResponse(html.replace(marker, injected, 1))

    # Mounted after the route above so "/" keeps its injected copy; StaticFiles
    # would otherwise serve the file verbatim and the page would arrive with no
    # token and no way to say why its first call was refused.
    app.mount("/", StaticFiles(directory=root), name="ui")


app = create_app()
