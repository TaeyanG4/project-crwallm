"""The doors the screen needs onto things that were CLI-only.

Settings, model management, and proving a recipe all existed as commands and
nowhere else, which meant a window could show you a recipe but never check it,
and could not say which model it was about to use.

These are the checks that the new endpoints answer honestly - including when
the optional pieces are switched off, because "Ollama is not running" has to
be an answer rather than a stack trace on a screen whose other tabs work fine.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crwallm.api.app import create_app
from crwallm.api.security import TOKEN_HEADER
from crwallm.config import Settings
from tests.fixtures.malicious_server.server import MaliciousServer, RunningServer

pytestmark = pytest.mark.integration

TOKEN = "test-token"
SECRET = "hunter2"


@pytest.fixture(scope="module")
def server() -> Iterator[RunningServer]:
    s = MaliciousServer()
    try:
        yield s.start()
    finally:
        s.stop()


@pytest.fixture
def recipes_dir(tmp_path: Path) -> Path:
    return tmp_path / "recipes"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, recipes_dir: Path) -> Iterator[TestClient]:
    recipes_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        api_token=TOKEN,
        recipes_dir=recipes_dir,
        database_url=f"postgresql+asyncpg://crwallm:{SECRET}@localhost:5433/crwallm",
    )
    monkeypatch.setattr("crwallm.config.get_settings", lambda: settings)
    monkeypatch.setattr("crwallm.api.deps.get_settings", lambda: settings)
    with TestClient(create_app(settings), headers={"Host": "127.0.0.1"}) as c:
        c.headers[TOKEN_HEADER] = TOKEN
        yield c


class TestSettings:
    def test_it_reports_what_this_install_is_set_to(self, client: TestClient) -> None:
        info = client.get("/api/settings").json()

        assert info["api"].startswith("http://")
        assert info["llm_model"]
        assert info["recipes_dir"]

    def test_the_database_password_never_leaves(self, client: TestClient) -> None:
        """A settings screen that prints a live credential is a screen nobody
        can screenshot, and screenshots are how people ask for help."""
        body = client.get("/api/settings").text

        assert SECRET not in body
        assert "***" in client.get("/api/settings").json()["database"]

    def test_the_api_token_is_reported_as_a_yes_or_no(self, client: TestClient) -> None:
        info = client.get("/api/settings").json()

        assert info["api_token_set"] is True
        assert TOKEN not in client.get("/api/settings").text

    def test_the_engine_defaults_travel_with_it(self, client: TestClient) -> None:
        """The screen's own defaults come from here, so the two cannot drift."""
        limits = client.get("/api/settings").json()["limits"]

        assert limits["max_pages"] >= 1
        assert "global_concurrency" in limits
        assert "browser" in limits and "spider" in limits

    def test_it_is_not_writable(self, client: TestClient) -> None:
        """An endpoint that could rewrite the API's host or token is one that
        can lock you out of the window you are calling it from."""
        assert client.post("/api/settings", json={}).status_code in (404, 405)


class TestModels:
    def test_an_unreachable_model_server_is_an_answer(
        self, monkeypatch: pytest.MonkeyPatch, recipes_dir: Path
    ) -> None:
        """Not a 500. The model is optional, and every other tab keeps working
        without it - a settings screen that errors would say otherwise."""
        recipes_dir.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            api_token=TOKEN,
            recipes_dir=recipes_dir,
            # Nothing listens here.
            ollama_base_url="http://127.0.0.1:1",
        )
        monkeypatch.setattr("crwallm.config.get_settings", lambda: settings)
        monkeypatch.setattr("crwallm.api.deps.get_settings", lambda: settings)

        with TestClient(create_app(settings), headers={"Host": "127.0.0.1"}) as c:
            c.headers[TOKEN_HEADER] = TOKEN
            body = c.get("/api/models").json()

        assert body["reachable"] is False
        assert body["installed"] == []
        assert body["chosen"]

    def test_a_model_server_that_is_off_says_so_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch, recipes_dir: Path
    ) -> None:
        """503 with a sentence, not a 500 with a class name. Pressing 이걸로
        while Ollama is switched off is an ordinary thing to do."""
        recipes_dir.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            api_token=TOKEN, recipes_dir=recipes_dir, ollama_base_url="http://127.0.0.1:1"
        )
        monkeypatch.setattr("crwallm.config.get_settings", lambda: settings)
        monkeypatch.setattr("crwallm.api.deps.get_settings", lambda: settings)

        with TestClient(create_app(settings), headers={"Host": "127.0.0.1"}) as c:
            c.headers[TOKEN_HEADER] = TOKEN
            response = c.post("/api/models/use", json={"name": "nothing:here"})

        assert response.status_code == 503
        assert "Ollama" in response.json()["detail"]

    @pytest.mark.parametrize("name", ["../../etc/passwd", "a/../b", "", "has space", "-lead"])
    def test_a_name_that_is_not_a_model_name_is_refused(
        self, client: TestClient, name: str
    ) -> None:
        """Refused by the shape, before anything is asked to look it up.

        Nothing here opens a file by this name, but ``..`` means nothing to
        Ollama either - so allowing it buys nothing and costs whoever next
        reaches for the filesystem with it.
        """
        assert client.post("/api/models/use", json={"name": name}).status_code == 422

    @pytest.mark.parametrize("name", ["qwen3.5:9b", "library/bge-m3", "llama3.1:8b-instruct-q4"])
    def test_a_real_model_name_passes_the_shape(self, client: TestClient, name: str) -> None:
        """The names Ollama actually uses must survive the pattern."""
        assert client.post("/api/models/use", json={"name": name}).status_code != 422

    def test_the_writes_are_guarded(self, client: TestClient) -> None:
        naked = TestClient(create_app(Settings(api_token=TOKEN)), headers={"Host": "127.0.0.1"})

        assert naked.post("/api/models/pull", json={"name": "x"}).status_code == 401
        assert naked.delete("/api/models/x").status_code == 401


def write_recipe(directory: Path, url: str) -> None:
    """A recipe for the fixture's shop page, as a file on disk."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "shop.yaml").write_text(
        "\n".join(
            [
                "name: shop",
                "source: css",
                f"source_url: {url}",
                "allowed_domains: [127.0.0.1]",
                "container: li.product-item",
                "fields:",
                "  - {name: title, selector: 'h3.name'}",
                "  - {name: price, selector: 'span.price'}",
                "",
            ]
        ),
        encoding="utf-8",
    )


class TestProvingARecipe:
    @pytest.fixture(autouse=True)
    def _allow_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fixture server is on loopback, which the guard refuses by
        default - the same opt-in the CLI has (crwallm/policy/local.py)."""
        from crwallm.policy.local import build_guard

        monkeypatch.setattr(
            "crwallm.api.routers.recipes.build_guard",
            lambda **_: build_guard(allow_local=True),
            raising=False,
        )
        import crwallm.api.routers.recipes as module

        original = module._measure

        async def local(recipe: object, url: str | None) -> object:
            import crwallm.policy.local as policy

            real = policy.build_guard
            policy.build_guard = lambda **_: real(allow_local=True)  # type: ignore[assignment]
            try:
                return await original(recipe, url)  # type: ignore[arg-type]
            finally:
                policy.build_guard = real  # type: ignore[assignment]

        monkeypatch.setattr(module, "_measure", local)

    def test_testing_scores_without_changing_anything(
        self, client: TestClient, server: RunningServer, recipes_dir: Path
    ) -> None:
        write_recipe(recipes_dir, server.url("/shop"))

        report = client.post("/api/recipes/shop/test", json={}).json()

        assert report["record_count"] >= 8
        assert report["container_matched"] is True
        assert "title" in report["fill_rates"]
        # Unchanged on disk: testing is the thing you do *before* believing it.
        assert client.get("/api/recipes/shop").json()["status"] == "candidate"

    def test_activating_re_measures_and_promotes(
        self, client: TestClient, server: RunningServer, recipes_dir: Path
    ) -> None:
        write_recipe(recipes_dir, server.url("/shop"))

        report = client.post("/api/recipes/shop/activate", json={}).json()

        assert report["status"] == "active"
        assert client.get("/api/recipes/shop").json()["status"] == "active"
        assert client.get("/api/recipes/shop").json()["record_count"] >= 8

    def test_a_recipe_that_matches_nothing_cannot_be_activated(
        self, client: TestClient, server: RunningServer, recipes_dir: Path
    ) -> None:
        """The refusal is the point: 활성화 is a claim, and this is the gate
        that makes the claim mean something."""
        write_recipe(recipes_dir, server.url("/shop"))
        path = recipes_dir / "shop.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("li.product-item", "li.nothing-here"),
            encoding="utf-8",
        )

        response = client.post("/api/recipes/shop/activate", json={})

        assert response.status_code == 409
        assert client.get("/api/recipes/shop").json()["status"] == "candidate"

    def test_the_list_says_which_extractor_reads_the_records(
        self, client: TestClient, server: RunningServer, recipes_dir: Path
    ) -> None:
        """A recipe with no CSS selectors at all is a normal thing here - feed,
        table and microdata recipes have none - and a screen that cannot say
        which source it uses cannot explain why it works."""
        write_recipe(recipes_dir, server.url("/shop"))

        assert client.get("/api/recipes/shop").json()["source"] == "css"
        assert client.get("/api/recipes").json()[0]["source"] == "css"

    def test_an_unknown_recipe_is_a_404(self, client: TestClient) -> None:
        assert client.post("/api/recipes/nope/test", json={}).status_code == 404

    def test_proving_is_guarded(self, client: TestClient, recipes_dir: Path) -> None:
        naked = TestClient(
            create_app(Settings(api_token=TOKEN, recipes_dir=recipes_dir)),
            headers={"Host": "127.0.0.1"},
        )

        assert naked.post("/api/recipes/shop/test", json={}).status_code == 401
        assert naked.post("/api/recipes/shop/activate", json={}).status_code == 401
