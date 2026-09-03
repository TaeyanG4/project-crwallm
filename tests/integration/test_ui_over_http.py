"""The same four verbs, reached over HTTP instead of through a window.

The window and the browser are two hosts for one core. That only holds if the
HTTP side actually produces the same answers, so this drives the real routes
against the same adversarial fixture the bridge tests use, and asserts the
shape the page draws from.

It also covers the two things that are only true of the browser: the page is
served with a token written into it (the window needs none), and saving is a
download rather than a native dialog.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from crwallm.api.app import create_app
from crwallm.api.security import TOKEN_HEADER
from crwallm.config import Settings
from crwallm.services.quick import MAX_PREVIEW_ROWS
from tests.fixtures.malicious_server.server import MaliciousServer, RunningServer

pytestmark = pytest.mark.integration

TOKEN = "test-token"

LOOK_KEYS = {"ok", "url", "columns", "count", "hint"}
COLLECT_KEYS = {"ok", "rows", "total", "shown", "pages", "failed", "cancelled", "hint"}


@pytest.fixture(scope="module")
def server() -> Iterator[RunningServer]:
    s = MaliciousServer()
    try:
        yield s.start()
    finally:
        s.stop()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(api_token=TOKEN)
    monkeypatch.setattr("crwallm.config.get_settings", lambda: settings)
    monkeypatch.setattr("crwallm.api.deps.get_settings", lambda: settings)
    # The fixture server is on loopback, which the guard refuses by default -
    # the same opt-in the CLI has, and the reason it is not a config key is in
    # crwallm/policy/local.py.
    monkeypatch.setattr(
        "crwallm.api.routers.ui.build_guard",
        lambda **_: __import__("crwallm.policy.local", fromlist=["build_guard"]).build_guard(
            allow_local=True
        ),
    )
    # Host: the middleware rejects anything not on the allowlist, and
    # TestClient's default "testserver" is exactly the kind of name it exists
    # to refuse (crwallm/api/security.py).
    with TestClient(create_app(settings), headers={"Host": "127.0.0.1"}) as c:
        c.headers[TOKEN_HEADER] = TOKEN
        yield c


def sid(name: str) -> str:
    return f"test-{name}"


class TestThePageItself:
    def test_the_page_is_served_from_the_api(self, client: TestClient) -> None:
        """One port. There is no second server and no proxy in front."""
        response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "모으기" in response.text

    def test_the_page_carries_the_token_it_needs(self, client: TestClient) -> None:
        """Every call the page makes is guarded, so the page has to know it.

        Safe because a page on another origin cannot read this one's body -
        which is also why the token must never move to a URL or a header a
        cross-origin request could see.
        """
        body = client.get("/").text

        assert "window.CRWALLM_TOKEN" in body
        assert TOKEN in body

    def test_the_script_and_stylesheet_are_served_too(self, client: TestClient) -> None:
        for name in ("app.js", "style.css"):
            assert client.get(f"/{name}").status_code == 200, name

    def test_the_verbs_are_guarded(self, client: TestClient) -> None:
        """Without this a page on any site could drive the crawler."""
        naked = TestClient(create_app(Settings(api_token=TOKEN)), headers={"Host": "127.0.0.1"})
        response = naked.post("/api/ui/look", json={"sid": "x", "url": "https://example.test"})

        assert response.status_code == 401


class TestTheVerbs:
    def test_look_finds_the_same_columns_the_window_gets(
        self, client: TestClient, server: RunningServer
    ) -> None:
        found = client.post(
            "/api/ui/look", json={"sid": sid("look"), "url": server.url("/shop")}
        ).json()

        assert found["ok"] is True, found.get("error")
        assert set(found) >= LOOK_KEYS
        assert found["count"] >= 8
        assert found["columns"]
        assert all(c["suggested"] for c in found["columns"]), "every column arrives named"

    def test_a_refused_address_is_a_sentence_not_a_500(self, client: TestClient) -> None:
        failed = client.post(
            "/api/ui/look", json={"sid": sid("bad"), "url": "http://127.0.0.1:1/"}
        ).json()

        assert failed["ok"] is False
        assert failed["error"]

    def test_collect_returns_the_table_the_page_draws(
        self, client: TestClient, server: RunningServer
    ) -> None:
        s = sid("collect")
        url = server.url("/shop")
        found = client.post("/api/ui/look", json={"sid": s, "url": url}).json()

        out = client.post(
            "/api/ui/collect",
            json={
                "sid": s,
                "url": url,
                "picks": [{"index": found["columns"][0]["index"], "name": "제품"}],
                "max_pages": 1,
            },
        ).json()

        assert out["ok"] is True, out.get("error")
        assert set(out) >= COLLECT_KEYS
        assert out["rows"]
        assert {key for row in out["rows"] for key in row} == {"제품"}

    def test_the_response_never_carries_the_whole_table(
        self, client: TestClient, server: RunningServer
    ) -> None:
        """The screen gets a slice; the file gets everything. A response that
        quietly included every row would be the one place this could go wrong
        without anybody noticing until it was fifty thousand rows."""
        s = sid("slice")
        url = server.url("/shop")
        client.post("/api/ui/look", json={"sid": s, "url": url})
        out = client.post(
            "/api/ui/collect",
            json={"sid": s, "url": url, "picks": [{"index": 0, "name": "제품"}], "max_pages": 20},
        ).json()

        assert len(out["rows"]) <= MAX_PREVIEW_ROWS
        assert out["shown"] == len(out["rows"])
        assert out["total"] >= out["shown"]
        assert "all_rows" not in out

    def test_two_columns_cannot_share_a_name(
        self, client: TestClient, server: RunningServer
    ) -> None:
        s = sid("dupe")
        url = server.url("/shop")
        found = client.post("/api/ui/look", json={"sid": s, "url": url}).json()

        out = client.post(
            "/api/ui/collect",
            json={
                "sid": s,
                "url": url,
                "picks": [
                    {"index": found["columns"][0]["index"], "name": "값"},
                    {"index": found["columns"][1]["index"], "name": "값"},
                ],
                "max_pages": 1,
            },
        ).json()

        assert out["ok"] is False
        assert "값" in out["error"]


class TestSaving:
    def test_the_download_holds_every_row(self, client: TestClient, server: RunningServer) -> None:
        s = sid("save")
        url = server.url("/shop")
        client.post("/api/ui/look", json={"sid": s, "url": url})
        out = client.post(
            "/api/ui/collect",
            json={"sid": s, "url": url, "picks": [{"index": 0, "name": "제품"}], "max_pages": 20},
        ).json()

        response = client.post("/api/ui/save", json={"sid": s})

        assert response.status_code == 200
        assert response.content.startswith(b"\xef\xbb\xbf"), (
            "Excel reads a plain UTF-8 CSV as the system codepage; the BOM is what tells it"
        )
        lines = response.content.decode("utf-8-sig").strip().splitlines()
        assert len(lines) == out["total"] + 1

    def test_saving_nothing_says_so(self, client: TestClient) -> None:
        response = client.post("/api/ui/save", json={"sid": sid("empty")})

        assert response.status_code == 404
        assert response.json()["detail"]

    def test_one_tab_does_not_overwrite_another(
        self, client: TestClient, server: RunningServer
    ) -> None:
        """Two browser tabs are two sessions. Sharing one would mean saving in
        the first tab handed you the second tab's table."""
        url = server.url("/shop")
        for tab in ("tab-a", "tab-b"):
            client.post("/api/ui/look", json={"sid": tab, "url": url})
        client.post(
            "/api/ui/collect",
            json={
                "sid": "tab-a",
                "url": url,
                "picks": [{"index": 0, "name": "가"}],
                "max_pages": 1,
            },
        )

        assert client.post("/api/ui/save", json={"sid": "tab-a"}).status_code == 200
        assert client.post("/api/ui/save", json={"sid": "tab-b"}).status_code == 404
