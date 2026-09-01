"""Application settings.

Everything is env-driven with the ``CRWALLM_`` prefix. See ``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRWALLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- runtime ---------------------------------------------------------
    env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"

    # -- api -------------------------------------------------------------
    # Local-only by design. Binding to 0.0.0.0 exposes an unauthenticated
    # crawler to the network. See docs/11_SECURITY_MODEL.md §1.
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_token: str = Field(default="", description="Required for mutating endpoints")

    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "[::1]"],
        description="Host header allowlist — blocks DNS rebinding",
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"],
    )

    # -- database --------------------------------------------------------
    database_url: str = "postgresql+asyncpg://crwallm:crwallm@localhost:5432/crwallm"
    db_echo: bool = False

    # -- storage ---------------------------------------------------------
    archive_dir: Path = Path("./data/archive")

    @field_validator("api_host")
    @classmethod
    def _warn_on_public_bind(cls, v: str) -> str:
        if v not in {"127.0.0.1", "localhost", "::1"}:
            # Not fatal — the user may know what they are doing behind a proxy —
            # but it must be a deliberate choice, so make it loud.
            import warnings

            warnings.warn(
                f"api_host={v!r} exposes an unauthenticated crawler beyond localhost. "
                "See docs/11_SECURITY_MODEL.md",
                stacklevel=2,
            )
        return v

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
