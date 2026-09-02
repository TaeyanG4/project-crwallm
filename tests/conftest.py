"""Shared fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crwallm.api.app import create_app
from crwallm.config import Settings

TEST_TOKEN = "test-token-do-not-use-in-production"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="dev",
        api_token=TEST_TOKEN,
        database_url="postgresql+asyncpg://crwallm:crwallm@localhost:5433/crwallm_test",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), headers={"Host": "127.0.0.1"})
