"""The LLM runtime, without a model.

A fake gateway rather than a live one: these tests are about the machinery
around the model - routing, candidate scoring, the retry loop, what happens
when a proposal is nonsense - and that machinery must be judged on inputs it
would otherwise never see. Tests against the real model live in
``tests/integration/test_llm_live.py`` and skip when it is not running.

docs/08_LLM_ARCHITECTURE.md
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from crwallm.crawler.contracts import FetchResponse
from crwallm.llm.gateway import (
    GatewayError,
    GatewayHealth,
    GenerationOptions,
    ModelUnavailableError,
    StructuredResult,
    TaskKind,
    Usage,
)
from crwallm.llm.hardware import GpuInfo, HardwareProfile
from crwallm.llm.manager import CatalogEntry, ModelCatalog
from crwallm.llm.routing import (
    BackendConfig,
    RoutedGateway,
    RoutingConfig,
    SecretResolutionError,
    resolve_secret,
)
from crwallm.llm.tasks.adapt import ColumnNaming, NamedColumn, adapt_page, build_prompt
from crwallm.policy.url import normalize
from crwallm.schemas.types import FetchMode

LISTING = """<html><body><ul>
 <li class="item"><h3><a href="/p/1">Gaming laptop</a></h3><span class="price">1,290,000</span></li>
 <li class="item"><h3><a href="/p/2">Office laptop</a></h3><span class="price">890,000</span></li>
 <li class="item"><h3><a href="/p/3">Light laptop</a></h3><span class="price">2,490,000</span></li>
 <li class="item"><h3><a href="/p/4">Sold laptop</a></h3><span class="price">Sold out</span></li>
</ul></body></html>"""


def response(html: str = LISTING) -> FetchResponse:
    return FetchResponse(
        url=normalize("https://shop.test/list"),
        status=200,
        headers={"content-type": "text/html"},
        body=html.encode(),
        elapsed_ms=1,
        fetch_mode=FetchMode.HTTP,
    )


class FakeGateway:
    """Answers with whatever the test scripted.

    Scripted per call so a test can say "first proposal is wrong, second is
    right" - which is the case the retry loop exists for and the one a live
    model will not reliably reproduce.
    """

    name = "fake"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[str] = []
        self.closed = False

    async def generate_structured[M: BaseModel](
        self,
        *,
        task: TaskKind,
        prompt: str,
        schema: type[M],
        system: str | None = None,
        options: GenerationOptions | None = None,
        n: int = 1,
    ) -> list[StructuredResult[M]]:
        self.calls.append(prompt)
        if not self.script:
            raise GatewayError("fake: script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return [
            StructuredResult(value=item, usage=Usage(elapsed_ms=1, model="fake", backend="fake"))
        ]

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]

    async def health(self) -> GatewayHealth:
        return GatewayHealth(reachable=True, backend="fake")

    async def aclose(self) -> None:
        self.closed = True


def naming(**names: int) -> ColumnNaming:
    return ColumnNaming(fields=[NamedColumn(index=i, name=n) for n, i in names.items()])


# ------------------------------------------------------------- adaptation


class TestAdaptation:
    async def test_a_good_naming_produces_a_working_recipe(self) -> None:
        gateway = FakeGateway([naming(title=0, url=1, price=2)])
        outcome = await adapt_page(gateway, response(), name="shop", allowed_domains=("shop.test",))
        assert outcome.succeeded
        assert outcome.recipe is not None
        assert {f.name for f in outcome.recipe.fields} == {"title", "url", "price"}
        assert outcome.result is not None
        assert outcome.result.quality.record_count == 4

    async def test_the_model_never_supplies_a_selector(self) -> None:
        """It names indices; the selectors come from the detector. A
        hallucinated selector is therefore not a possible failure."""
        gateway = FakeGateway([naming(title=0)])
        outcome = await adapt_page(gateway, response(), name="shop", allowed_domains=("shop.test",))
        assert outcome.recipe is not None
        assert outcome.recipe.fields[0].selector  # detector-supplied, non-empty
        assert "h3" in outcome.recipe.fields[0].selector

    async def test_indices_that_do_not_exist_are_dropped(self) -> None:
        gateway = FakeGateway([naming(title=0, nonsense=99)])
        outcome = await adapt_page(gateway, response(), name="shop", allowed_domains=("shop.test",))
        assert outcome.recipe is not None
        assert {f.name for f in outcome.recipe.fields} == {"title"}

    async def test_a_wholly_invalid_proposal_triggers_a_retry(self) -> None:
        """The point of the loop: a first answer that names nothing real is
        told so, and asked again."""
        gateway = FakeGateway([naming(a=90, b=91), naming(title=0, price=2)])
        outcome = await adapt_page(
            gateway, response(), name="shop", allowed_domains=("shop.test",), candidates=1
        )
        assert outcome.rounds == 2
        assert outcome.succeeded

    async def test_failure_is_fed_back_to_the_model(self) -> None:
        gateway = FakeGateway([naming(a=90), naming(title=0)])
        await adapt_page(
            gateway, response(), name="shop", allowed_domains=("shop.test",), candidates=1
        )
        assert len(gateway.calls) == 2
        assert "did not work" in gateway.calls[1]

    async def test_the_best_scoring_proposal_wins(self) -> None:
        """Two valid answers, one better. The scorer chooses, not the model."""
        gateway = FakeGateway([naming(title=0), naming(title=0, url=1, price=2)])
        outcome = await adapt_page(
            gateway, response(), name="shop", allowed_domains=("shop.test",), candidates=2
        )
        assert outcome.recipe is not None
        # The first already passes, so the loop stops - paying for a second
        # generation when the first works is the thing the early exit removed.
        assert len(gateway.calls) == 1

    async def test_a_page_with_no_structure_says_so(self) -> None:
        gateway = FakeGateway([])
        outcome = await adapt_page(
            gateway,
            response("<html><body><h1>One thing</h1></body></html>"),
            name="detail",
            allowed_domains=("shop.test",),
        )
        assert not outcome.succeeded
        assert outcome.recipe is None
        assert "detail page" in outcome.attempts[0]
        assert gateway.calls == [], "a page with nothing to name must not cost a generation"

    async def test_a_gateway_failure_ends_the_loop(self) -> None:
        gateway = FakeGateway([ModelUnavailableError("no server")])
        outcome = await adapt_page(gateway, response(), name="shop", allowed_domains=("shop.test",))
        assert not outcome.succeeded
        assert "no server" in outcome.attempts[0]

    async def test_usage_is_accumulated(self) -> None:
        gateway = FakeGateway([naming(a=90), naming(title=0)])
        outcome = await adapt_page(
            gateway, response(), name="shop", allowed_domains=("shop.test",), candidates=1
        )
        assert len(outcome.usage) == 2


class TestPrompt:
    def test_the_prompt_carries_samples_not_markup(self) -> None:
        """The model is asked to name values, so it gets values. A reduced DOM
        would be more context for a worse question."""
        from crwallm.crawler.extraction.css import parse
        from crwallm.structure.detector import detect_containers

        tree, _ = parse(response())
        prompt = build_prompt(detect_containers(tree)[0])
        assert "Gaming laptop" in prompt
        assert "[0]" in prompt
        assert "<li" not in prompt


# ----------------------------------------------------------------- routing


class TestRouting:
    def test_every_task_routes_by_default(self) -> None:
        config = RoutingConfig.local_default()
        for task in TaskKind:
            assert config.backend_for(task).name == "local"

    def test_tasks_can_be_split_across_backends(self) -> None:
        """The practical answer to a small GPU: easy work local, hard work
        elsewhere, as configuration rather than a code path."""
        config = RoutingConfig(
            backends={
                "local": BackendConfig(name="local", model="qwen3.5:4b"),
                "api": BackendConfig(name="api", kind="openai_compat", model="gpt-4.1-mini"),
            },
            tasks={TaskKind.COMPILE_SPEC: "local", TaskKind.ADAPT_SELECTORS: "api"},
            fallback="local",
        )
        assert config.backend_for(TaskKind.COMPILE_SPEC).model == "qwen3.5:4b"
        assert config.backend_for(TaskKind.ADAPT_SELECTORS).model == "gpt-4.1-mini"
        assert config.backend_for(TaskKind.CLASSIFY).name == "local"

    async def test_fallback_covers_unavailability(self, monkeypatch: pytest.MonkeyPatch) -> None:
        primary = FakeGateway([ModelUnavailableError("down")])
        secondary = FakeGateway([naming(title=0)])

        config = RoutingConfig(
            backends={
                "a": BackendConfig(name="a"),
                "b": BackendConfig(name="b"),
            },
            tasks={TaskKind.ADAPT_SELECTORS: "a"},
            fallback="b",
        )
        routed = RoutedGateway(config)
        routed._built = {"a": primary, "b": secondary}

        results = await routed.generate_structured(
            task=TaskKind.ADAPT_SELECTORS, prompt="x", schema=ColumnNaming
        )
        assert results
        assert secondary.calls

    async def test_fallback_does_not_cover_a_bad_answer(self) -> None:
        """Escalating because a local answer scored low is a way to spend money
        on a judgement the caller never made. That is the scoring loop's job."""
        primary = FakeGateway([GatewayError("nothing validated")])
        secondary = FakeGateway([naming(title=0)])

        routed = RoutedGateway(
            RoutingConfig(
                backends={"a": BackendConfig(name="a"), "b": BackendConfig(name="b")},
                tasks={TaskKind.ADAPT_SELECTORS: "a"},
                fallback="b",
            )
        )
        routed._built = {"a": primary, "b": secondary}

        with pytest.raises(GatewayError):
            await routed.generate_structured(
                task=TaskKind.ADAPT_SELECTORS, prompt="x", schema=ColumnNaming
            )
        assert secondary.calls == []


class TestSecrets:
    def test_env_references_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_KEY", "sk-abc")
        assert resolve_secret("env:SOME_KEY") == "sk-abc"

    def test_a_missing_variable_says_which_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ABSENT_KEY", raising=False)
        with pytest.raises(SecretResolutionError, match="ABSENT_KEY"):
            resolve_secret("env:ABSENT_KEY")

    def test_only_references_are_accepted(self) -> None:
        """A literal key in config gets committed."""
        with pytest.raises(SecretResolutionError, match="env:NAME"):
            resolve_secret("sk-literal-key-in-the-config-file")

    def test_no_reference_is_not_an_error(self) -> None:
        assert resolve_secret(None) is None


# ---------------------------------------------------------------- options


class TestGenerationOptions:
    def test_thinking_is_off_by_default(self) -> None:
        """Measured on qwen3:14b: 69s with, 2.0s without, same question."""
        assert GenerationOptions().think is False

    def test_context_is_set_explicitly(self) -> None:
        """Ollama's default is often 2048 and exceeding it truncates silently."""
        assert GenerationOptions().num_ctx >= 8192

    def test_the_model_stays_resident_by_default(self) -> None:
        assert GenerationOptions().keep_alive == -1

    def test_with_copies_rather_than_mutating(self) -> None:
        base = GenerationOptions()
        derived = base.with_(temperature=0.9)
        assert base.temperature != 0.9
        assert derived.temperature == 0.9

    def test_extra_is_not_shared_between_copies(self) -> None:
        base = GenerationOptions()
        derived = base.with_(temperature=0.5)
        derived.extra["x"] = 1
        assert base.extra == {}


# --------------------------------------------------------------- hardware


class TestCatalogFit:
    @staticmethod
    def profile(vram_gb: float, ram_gb: float = 32.0) -> HardwareProfile:
        gpus = (GpuInfo("test", int(vram_gb * 1024)),) if vram_gb else ()
        return HardwareProfile(gpus=gpus, system_ram_gb=ram_gb)

    def test_usable_vram_leaves_headroom(self) -> None:
        """A model sized to the nameplate figure does not load - the KV cache
        and whatever else is resident want their share."""
        assert self.profile(16).usable_vram_gb < 16

    def test_a_model_larger_than_the_card_does_not_fit(self) -> None:
        entry = CatalogEntry(name="big", tasks=("x",), min_vram_gb=20, size_gb=17)
        assert not entry.fits(self.profile(16))

    def test_a_model_that_fits_is_accepted(self) -> None:
        entry = CatalogEntry(name="ok", tasks=("x",), min_vram_gb=9, size_gb=6.6)
        assert entry.fits(self.profile(16))

    def test_without_a_gpu_the_ceiling_is_system_ram(self) -> None:
        entry = CatalogEntry(name="ok", tasks=("x",), min_vram_gb=9, size_gb=6.6)
        assert entry.fits(self.profile(0, ram_gb=32))
        assert not entry.fits(self.profile(0, ram_gb=8))

    def test_the_recommendation_is_the_largest_that_fits(self) -> None:
        catalog = ModelCatalog(
            (
                CatalogEntry(name="small", tasks=("adapt_selectors",), min_vram_gb=3, size_gb=1.6),
                CatalogEntry(name="mid", tasks=("adapt_selectors",), min_vram_gb=9, size_gb=6.6),
                CatalogEntry(name="huge", tasks=("adapt_selectors",), min_vram_gb=40, size_gb=30),
            ),
            {},
        )
        assert catalog.recommend(self.profile(16)) is not None
        assert catalog.recommend(self.profile(16)).name == "mid"  # type: ignore[union-attr]

    def test_nothing_fitting_returns_none(self) -> None:
        catalog = ModelCatalog(
            (CatalogEntry(name="huge", tasks=("adapt_selectors",), min_vram_gb=40, size_gb=30),),
            {},
        )
        assert catalog.recommend(self.profile(0, ram_gb=4)) is None


class TestShippedCatalog:
    def test_models_toml_parses(self) -> None:
        from pathlib import Path

        catalog = ModelCatalog.load(Path("models.toml"))
        assert catalog.entries
        assert catalog.default_entry is not None

    def test_thinking_is_off_in_the_shipped_defaults(self) -> None:
        from pathlib import Path

        assert ModelCatalog.load(Path("models.toml")).defaults.get("think") is False

    def test_an_embedding_model_is_listed(self) -> None:
        from pathlib import Path

        assert ModelCatalog.load(Path("models.toml")).for_task("embed")
