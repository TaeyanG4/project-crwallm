"""Local API hardening — docs/11_SECURITY_MODEL.md §1.

These guard the boundary that makes an unauthenticated localhost API safe.
If one of these ever goes red, a web page the user visits can drive the
crawler.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from crwallm.api.security import TOKEN_HEADER, make_token_dependency
from crwallm.config import Settings
from tests.conftest import TEST_TOKEN


class TestHostHeader:
    def test_localhost_is_allowed(self, client: TestClient) -> None:
        assert client.get("/health", headers={"Host": "localhost"}).status_code == 200

    def test_loopback_ip_is_allowed(self, client: TestClient) -> None:
        assert client.get("/health", headers={"Host": "127.0.0.1"}).status_code == 200

    def test_port_suffix_is_ignored(self, client: TestClient) -> None:
        assert client.get("/health", headers={"Host": "127.0.0.1:8000"}).status_code == 200

    @pytest.mark.parametrize(
        "host",
        [
            "evil.example.com",  # plain cross-origin attempt
            "rebind.attacker.test",  # DNS rebinding to 127.0.0.1
            "127.0.0.1.attacker.test",  # lookalike prefix
        ],
    )
    def test_foreign_host_is_rejected(self, client: TestClient, host: str) -> None:
        r = client.get("/health", headers={"Host": host})
        assert r.status_code == 421


class TestToken:
    @staticmethod
    def _app(settings: Settings) -> TestClient:
        app = FastAPI()
        require = make_token_dependency(settings)

        @app.post("/mutate", dependencies=[Depends(require)])
        def mutate() -> dict[str, bool]:
            return {"ok": True}

        return TestClient(app)

    def test_valid_token_passes(self, settings: Settings) -> None:
        c = self._app(settings)
        assert c.post("/mutate", headers={TOKEN_HEADER: TEST_TOKEN}).status_code == 200

    def test_missing_token_is_rejected(self, settings: Settings) -> None:
        assert self._app(settings).post("/mutate").status_code == 401

    def test_wrong_token_is_rejected(self, settings: Settings) -> None:
        c = self._app(settings)
        assert c.post("/mutate", headers={TOKEN_HEADER: "nope"}).status_code == 401

    def test_unconfigured_token_fails_closed(self) -> None:
        """No token configured must not mean "everything allowed"."""
        c = self._app(Settings(api_token=""))
        assert c.post("/mutate", headers={TOKEN_HEADER: "anything"}).status_code == 500


class TestBindHost:
    def test_public_bind_warns(self) -> None:
        with pytest.warns(UserWarning, match="beyond localhost"):
            Settings(api_host="0.0.0.0")

    def test_loopback_bind_is_silent(self) -> None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Settings(api_host="127.0.0.1")
