"""Against a real model, when one is running.

The unit tests use a fake gateway, which proves the machinery but not that the
model actually answers. These check the parts that only a live model can:
constrained decoding really constrains, thinking really is off, and a 9B model
really does name the columns correctly.

Skipped when no server answers. That keeps the suite runnable without a GPU,
and means the skip count is worth reading - silently not running is different
from passing.

docs/08_LLM_ARCHITECTURE.md
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio

from crwallm.crawler.contracts import FetchFailure, FetchRequest, FetchResponse
from crwallm.crawler.fetching.http import SafeHttpFetcher
from crwallm.llm.gateway import GenerationOptions, TaskKind
from crwallm.llm.manager import ModelManager
from crwallm.llm.openai_compat import OllamaGateway
from crwallm.llm.tasks.adapt import ColumnNaming, adapt_page
from crwallm.policy.ssrf import SsrfGuard, StaticResolver
from crwallm.policy.url import normalize
from crwallm.schemas.types import FetchMode
from tests.fixtures.malicious_server.server import MaliciousServer, RunningServer

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

BASE_URL = os.environ.get("CRWALLM_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("CRWALLM_LLM_MODEL", "qwen3.5:9b")
LOOPBACK = [ipaddress.ip_network("127.0.0.0/8")]


def _root(url: str) -> str:
    trimmed = url.rstrip("/")
    return trimmed[: -len("/v1")] if trimmed.endswith("/v1") else trimmed


async def _model_available() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{_root(BASE_URL)}/api/tags")
        names = {m.get("name") for m in response.json().get("models", [])}
        return MODEL in names or f"{MODEL}:latest" in names
    except Exception:
        return False


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def gateway() -> AsyncIterator[OllamaGateway]:
    if not await _model_available():
        pytest.skip(f"{MODEL} not available at {_root(BASE_URL)}")
    client = OllamaGateway(base_url=BASE_URL, model=MODEL)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture(scope="module")
def server() -> Iterator[RunningServer]:
    s = MaliciousServer()
    try:
        yield s.start()
    finally:
        s.stop()


@pytest_asyncio.fixture(loop_scope="module")
async def shop_page(server: RunningServer) -> FetchResponse:
    guard = SsrfGuard(StaticResolver({}), allow_networks=LOOPBACK)  # type: ignore[arg-type]
    fetcher = SafeHttpFetcher(guard, http2=False)
    try:
        outcome = await fetcher.fetch(
            FetchRequest(
                url=normalize(server.url("/shop")),
                depth=0,
                mode=FetchMode.HTTP,
                timeout_s=15,
                byte_limit=5_000_000,
            )
        )
    finally:
        await fetcher.aclose()
    assert not isinstance(outcome, FetchFailure), outcome
    return outcome


class TestGateway:
    async def test_health_reports_the_model(self, gateway: OllamaGateway) -> None:
        health = await gateway.health()
        assert health.reachable
        assert any(MODEL in m for m in health.models)

    async def test_structured_output_is_actually_structured(self, gateway: OllamaGateway) -> None:
        """Constrained decoding, not a request to please answer in JSON. The
        difference is whether malformed output is impossible or merely
        unlikely."""
        results = await gateway.generate_structured(
            task=TaskKind.ADAPT_SELECTORS,
            prompt=(
                "A listing has these columns:\n"
                "[0] h3  (text, 100%)  ->  Gaming laptop\n"
                "[1] span.price  (text, 100%)  ->  1,290,000\n"
                "Name them."
            ),
            schema=ColumnNaming,
            options=GenerationOptions(num_predict=200),
        )
        assert results
        assert results[0].value.fields

    async def test_thinking_off_is_dramatically_faster(self, gateway: OllamaGateway) -> None:
        """The measurement behind the default: 69s versus 2.0s on qwen3:14b.

        The threshold is loose because the point is the order of magnitude,
        not the number - a machine under load should not turn this red.
        """
        prompt = "A column shows: 1,290,000 / 890,000 / Sold out. Name it."
        results = await gateway.generate_structured(
            task=TaskKind.CLASSIFY,
            prompt=prompt,
            schema=ColumnNaming,
            options=GenerationOptions(think=False, num_predict=150),
        )
        assert results
        assert results[0].usage.elapsed_ms < 30_000

    async def test_embeddings_come_back_the_right_shape(self, gateway: OllamaGateway) -> None:
        embed_model = os.environ.get("CRWALLM_EMBED_MODEL", "bge-m3")
        manager = ModelManager(BASE_URL)
        try:
            if not await manager.has(embed_model):
                pytest.skip(f"{embed_model} not installed")
        finally:
            await manager.aclose()

        vectors = await gateway.embed(["노트북 가격", "laptop price"], model=embed_model)
        assert len(vectors) == 2
        assert len(vectors[0]) == len(vectors[1]) > 100


class TestAdaptationLive:
    async def test_the_model_names_the_columns_correctly(
        self, gateway: OllamaGateway, shop_page: FetchResponse
    ) -> None:
        """The end of the chain: a page in, a working recipe out, no selector
        written by anyone.

        The names are checked loosely - "product_title" is as correct as
        "title", and pinning the exact string would make this a test of one
        model's vocabulary.
        """
        outcome = await adapt_page(
            gateway, shop_page, name="live-shop", allowed_domains=("127.0.0.1",)
        )

        assert outcome.succeeded, outcome.attempts
        assert outcome.recipe is not None
        assert outcome.result is not None
        assert outcome.result.quality.record_count == 8
        assert outcome.result.quality.mean_fill >= 0.9

        names = " ".join(f.name for f in outcome.recipe.fields)
        assert "title" in names or "name" in names
        assert "price" in names or "cost" in names

    async def test_selectors_come_from_the_detector(
        self, gateway: OllamaGateway, shop_page: FetchResponse
    ) -> None:
        """Whatever the model says, the selectors are ones the detector found
        on this page - so a hallucinated selector is not a failure mode."""
        from crwallm.crawler.extraction.css import parse
        from crwallm.structure.detector import detect_containers

        tree, _ = parse(shop_page)
        detected = {c.selector for c in detect_containers(tree)[0].usable_columns}

        outcome = await adapt_page(
            gateway, shop_page, name="live-shop", allowed_domains=("127.0.0.1",)
        )
        assert outcome.recipe is not None
        for rule in outcome.recipe.fields:
            assert rule.selector in detected

    async def test_one_round_is_usually_enough(
        self, gateway: OllamaGateway, shop_page: FetchResponse
    ) -> None:
        """Not a correctness requirement - the loop exists for when it is not.
        But a clean listing needing three rounds would mean the prompt or the
        detector had regressed."""
        outcome = await adapt_page(
            gateway, shop_page, name="live-shop", allowed_domains=("127.0.0.1",)
        )
        assert outcome.rounds == 1
        assert len(outcome.usage) <= 2, "an easy page should not need several proposals"
