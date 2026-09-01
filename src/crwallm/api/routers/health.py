"""Health and readiness."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from crwallm import __version__
from crwallm.api.deps import session_dep

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok"]
    version: str


class Readiness(BaseModel):
    status: Literal["ready", "degraded"]
    version: str
    database: bool


@router.get("/health", response_model=Health)
async def health() -> Health:
    """Liveness — no dependencies touched."""
    return Health(status="ok", version=__version__)


@router.get("/ready", response_model=Readiness)
async def ready(session: Annotated[AsyncSession, Depends(session_dep)]) -> Readiness:
    """Readiness — verifies the database is reachable."""
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # readiness must never raise
        db_ok = False
    return Readiness(
        status="ready" if db_ok else "degraded",
        version=__version__,
        database=db_ok,
    )
