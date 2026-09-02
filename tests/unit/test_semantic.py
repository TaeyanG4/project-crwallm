"""Filtering by meaning.

The interesting cases are the failure ones. A crawl runs unattended, so what
this does when the model is absent, slow or wrong decides whether a recipe is
safe to leave running - and "dropped everything because embedding failed" is
the outcome that looks like success from a distance.

docs/06_EXTRACTION_ARCHITECTURE.md
"""

from __future__ import annotations

from typing import Any

import pytest

from crwallm.llm.gateway import GatewayError
from crwallm.schemas.filters import RecordFilter
from crwallm.services.semantic import (
    SemanticFilterError,
    SemanticScorer,
    apply_all_filters,
    cosine,
)


class FakeGateway:
    """Embeddings from a fixed vocabulary, so similarity is predictable.

    Each text becomes counts over a handful of topic words. Two strings about
    laptops land near each other and neither lands near one about cooking,
    which is the property under test - not any particular model's numbers.
    """

    name = "fake"
    VOCAB = ("laptop", "computer", "keyboard", "recipe", "cooking", "pasta")

    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.fail = False

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if self.fail:
            raise GatewayError("no model")
        self.batches.append(list(texts))
        return [[float(text.lower().count(word)) for word in self.VOCAB] or [0.0] for text in texts]

    async def generate_structured(self, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def health(self) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover - unused
        return None


def scorer() -> SemanticScorer:
    return SemanticScorer(gateway=FakeGateway())  # type: ignore[arg-type]


RECORDS = [
    {"title": "laptop keyboard computer review", "price": 1_290_000},
    {"title": "pasta recipe cooking guide", "price": 12_000},
    {"title": "laptop computer buying advice", "price": 2_400_000},
]


class TestCosine:
    def test_identical_vectors_are_one(self) -> None:
        assert cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_are_zero(self) -> None:
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_a_zero_vector_scores_zero_rather_than_dividing(self) -> None:
        assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_mismatched_lengths_score_zero(self) -> None:
        """A model change mid-run would otherwise raise inside a crawl."""
        assert cosine([1.0], [1.0, 2.0]) == 0.0

    def test_opposite_vectors_are_negative(self) -> None:
        """Not clamped: negative similarity is a signal, not a floor."""
        assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


class TestSemanticFiltering:
    async def test_records_that_match_are_kept(self) -> None:
        result = await apply_all_filters(
            RECORDS,
            [RecordFilter(field="title", op="semantic", value="laptop computer", threshold=0.5)],
            scorer=scorer(),
        )
        assert [r["title"] for r in result.kept] == [
            "laptop keyboard computer review",
            "laptop computer buying advice",
        ]

    def test_the_dropped_reason_names_the_field(self) -> None:
        """ "12 dropped" is not actionable; "12 dropped by title semantic" is."""
        import asyncio

        result = asyncio.run(
            apply_all_filters(
                RECORDS,
                [RecordFilter(field="title", op="semantic", value="pasta", threshold=0.5)],
                scorer=scorer(),
            )
        )
        assert result.reasons == {"title semantic": 2}

    async def test_a_higher_threshold_narrows(self) -> None:
        loose = await apply_all_filters(
            RECORDS,
            [RecordFilter(field="title", op="semantic", value="laptop", threshold=0.3)],
            scorer=scorer(),
        )
        tight = await apply_all_filters(
            RECORDS,
            [RecordFilter(field="title", op="semantic", value="laptop", threshold=0.95)],
            scorer=scorer(),
        )
        assert len(tight.kept) < len(loose.kept)

    async def test_a_star_field_reads_the_whole_record(self) -> None:
        """The topic is in the title on one site and in a summary on the next."""
        records = [{"name": "X1", "summary": "a laptop computer keyboard"}]
        result = await apply_all_filters(
            records,
            [RecordFilter(field="*", op="semantic", value="laptop computer", threshold=0.5)],
            scorer=scorer(),
        )
        assert result.kept == records

    async def test_a_missing_field_drops_the_record(self) -> None:
        """A filter exists to narrow; a record it cannot judge is not one it
        should wave through."""
        result = await apply_all_filters(
            [{"other": "laptop"}],
            [RecordFilter(field="title", op="semantic", value="laptop", threshold=0.1)],
            scorer=scorer(),
        )
        assert result.kept == []


class TestOrdering:
    async def test_cheap_filters_run_first(self) -> None:
        """The whole reason for the split: the deterministic rule removes two
        records and only one is ever embedded."""
        gateway = FakeGateway()
        result = await apply_all_filters(
            RECORDS,
            [
                RecordFilter(field="price", op="lt", value=1_500_000),
                RecordFilter(field="title", op="semantic", value="laptop", threshold=0.3),
            ],
            scorer=SemanticScorer(gateway=gateway),  # type: ignore[arg-type]
        )
        embedded = [t for batch in gateway.batches for t in batch]
        assert len(embedded) <= 3, embedded
        assert len(result.kept) == 1

    async def test_repeated_text_is_embedded_once(self) -> None:
        gateway = FakeGateway()
        same = [{"title": "laptop computer"} for _ in range(5)]
        await apply_all_filters(
            same,
            [RecordFilter(field="title", op="semantic", value="laptop", threshold=0.3)],
            scorer=SemanticScorer(gateway=gateway),  # type: ignore[arg-type]
        )
        embedded = [t for batch in gateway.batches for t in batch]
        assert embedded.count("laptop computer") == 1

    async def test_one_batched_call_not_one_per_record(self) -> None:
        gateway = FakeGateway()
        await apply_all_filters(
            RECORDS,
            [RecordFilter(field="title", op="semantic", value="laptop", threshold=0.3)],
            scorer=SemanticScorer(gateway=gateway),  # type: ignore[arg-type]
        )
        assert len(gateway.batches) == 1


class TestDegradation:
    async def test_no_scorer_skips_the_semantic_rule(self) -> None:
        """A recipe carrying one must still run where no model is configured.
        Dropping every record because a model is missing is the worst of the
        available answers, and it looks like a working filter from outside."""
        result = await apply_all_filters(
            RECORDS,
            [RecordFilter(field="title", op="semantic", value="laptop", threshold=0.9)],
            scorer=None,
        )
        assert len(result.kept) == 3

    async def test_no_scorer_still_applies_the_cheap_rules(self) -> None:
        result = await apply_all_filters(
            RECORDS,
            [
                RecordFilter(field="price", op="lt", value=100_000),
                RecordFilter(field="title", op="semantic", value="laptop", threshold=0.9),
            ],
            scorer=None,
        )
        assert [r["price"] for r in result.kept] == [12_000]

    async def test_an_embedding_failure_is_raised_not_silently_dropped(self) -> None:
        """The caller decides whether to abandon the crawl. Quietly keeping
        everything would produce unfiltered output that looks filtered."""
        gateway = FakeGateway()
        gateway.fail = True
        with pytest.raises(SemanticFilterError, match="embedding failed"):
            await apply_all_filters(
                RECORDS,
                [RecordFilter(field="title", op="semantic", value="laptop")],
                scorer=SemanticScorer(gateway=gateway),  # type: ignore[arg-type]
            )

    async def test_a_short_reply_from_the_model_is_refused(self) -> None:
        """Zipping a short vector list against the texts would pair every
        record with the wrong embedding and filter on nonsense."""

        class Short(FakeGateway):
            async def embed(
                self, texts: list[str], *, model: str | None = None
            ) -> list[list[float]]:
                return [[1.0]]

        with pytest.raises(SemanticFilterError, match="got 1"):
            await apply_all_filters(
                RECORDS,
                [RecordFilter(field="title", op="semantic", value="laptop")],
                scorer=SemanticScorer(gateway=Short()),  # type: ignore[arg-type]
            )

    async def test_no_records_means_no_model_call(self) -> None:
        gateway = FakeGateway()
        await apply_all_filters(
            [],
            [RecordFilter(field="title", op="semantic", value="laptop")],
            scorer=SemanticScorer(gateway=gateway),  # type: ignore[arg-type]
        )
        assert gateway.batches == []
