"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Header
from sqlalchemy.ext.asyncio import AsyncSession

from crwallm.api.security import TOKEN_HEADER, make_token_dependency
from crwallm.config import Settings, get_settings
from crwallm.db.session import get_sessionmaker

__all__ = ["session_dep", "settings_dep", "token_dep"]


def settings_dep() -> Settings:
    return get_settings()


async def session_dep() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def token_dep(token: str | None = Header(default=None, alias=TOKEN_HEADER)) -> None:
    """Guards every mutating route.

    Resolves the settings per call rather than closing over them at import
    time, so tests can override ``settings_dep`` and so a token rotated in the
    environment does not need a restart.
    """
    make_token_dependency(get_settings())(token)
