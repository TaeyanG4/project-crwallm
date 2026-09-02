"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Header
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crwallm.api.security import TOKEN_HEADER, make_token_dependency
from crwallm.config import Settings, get_settings
from crwallm.db.session import get_sessionmaker

__all__ = ["session_dep", "sessionmaker_dep", "settings_dep", "token_dep"]


def settings_dep() -> Settings:
    return get_settings()


async def session_dep() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def sessionmaker_dep() -> async_sessionmaker[AsyncSession]:
    """For handlers that outlive their request.

    A streaming response keeps producing after the handler has returned, so it
    cannot hold ``session_dep``'s session - that one is closed on the way out,
    and holding it open instead would keep a connection checked out of the
    pool for the whole life of the stream. Such a handler needs the factory
    and opens sessions of its own.

    A dependency rather than a direct ``get_sessionmaker()`` call so a test
    can point the stream at its own engine; reaching past the injection point
    is what made this untestable in the first place."""
    return get_sessionmaker()


def token_dep(token: str | None = Header(default=None, alias=TOKEN_HEADER)) -> None:
    """Guards every mutating route.

    Resolves the settings per call rather than closing over them at import
    time, so tests can override ``settings_dep`` and so a token rotated in the
    environment does not need a restart.
    """
    make_token_dependency(get_settings())(token)
