from __future__ import annotations

from fastapi.testclient import TestClient

from crwallm import __version__


def test_health_is_live_without_database(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}
