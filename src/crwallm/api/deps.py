"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from crwallm.config import Settings, get_settings
from crwallm.db.session import get_sessionmaker


def settings_dep() -> Settings:
    return get_settings()


async def session_dep() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
