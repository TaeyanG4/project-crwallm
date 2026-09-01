"""Async engine and session factory.

Single engine per process. The worker and the API each get their own.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from crwallm.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        # Bulk inserts go through asyncpg COPY (Phase 2), not the ORM,
        # so the pool stays modest.
        pool_size=10,
        max_overflow=10,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


async def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
