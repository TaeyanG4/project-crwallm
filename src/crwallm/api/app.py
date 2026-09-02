"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from crwallm import __version__
from crwallm.api.routers import health, jobs, recipes
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
    return app


app = create_app()
