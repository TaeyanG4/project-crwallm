"""Filtering records by what they mean rather than what they contain.

The gap this closes was named by a plain question at the outset - can it
collect only the videos I want? - and ``contains`` is not an answer to it.
"tutorial" does not match "a walkthrough for beginners", and a list of
synonyms long enough to work is a list nobody will maintain.

**Embeddings, not a language model, for the filter itself.** A record either
resembles a description or it does not, and that is a distance between two
vectors. Asking a chat model to judge each row costs a generation per record
and gives a different answer on a rerun; an embedding is one small call per
distinct string and is stable, so a crawl that filtered 900 rows yesterday
filters the same 900 today.

**Cheap filters first.** ``apply_filters`` already splits deterministic rules
from this one. Running "under two million won" before "looks like a laptop
review" is the difference between embedding forty records and embedding four
thousand (docs/06_EXTRACTION_ARCHITECTURE.md).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from crwallm.llm.gateway import GatewayError, ModelGateway
from crwallm.schemas.filters import FilterResult, RecordFilter, apply_filters

__all__ = [
    "RecordSieve",
    "SemanticFilterError",
    "SemanticScorer",
    "apply_all_filters",
    "cosine",
]

MAX_TEXT_CHARS = 800
"""How much of a field to embed.

Embedding models truncate anyway, and the opening of a title or description
carries the topic. Sending a whole article body would be slower for an answer
that does not change."""


class SemanticFilterError(RuntimeError):
    """The filter could not be evaluated. Never raised at a record."""


def cosine(a: list[float], b: list[float]) -> float:
    """Similarity in ``[-1, 1]``, or 0.0 for a degenerate vector.

    Not normalised into ``[0, 1]``: a threshold is easier to reason about when
    the number means what the literature says it means, and negative
    similarity is a real signal rather than a floor to clamp away.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass(slots=True)
class SemanticScorer:
    """Embeds strings, remembering what it has already embedded.

    The cache is per-run and keyed by the exact text. On a listing crawl the
    same phrases recur constantly - a category label, a repeated
    manufacturer - and each one costs a network round trip the first time and
    nothing afterwards.
    """

    gateway: ModelGateway
    model: str | None = None
    _cache: dict[str, list[float]] = field(default_factory=dict)
    calls: int = 0
    embedded: int = 0

    async def embed_all(self, texts: list[str]) -> None:
        """Fetch and cache every vector not already held.

        One batched call rather than one per text: the round trip dominates,
        and a page of forty records is forty round trips done wrong.
        """
        missing = list(dict.fromkeys(t for t in texts if t and t not in self._cache))
        if not missing:
            return

        try:
            vectors = await self.gateway.embed(missing, model=self.model)
        except GatewayError as exc:
            raise SemanticFilterError(f"embedding failed: {exc}") from exc

        if len(vectors) != len(missing):
            raise SemanticFilterError(f"asked for {len(missing)} embeddings and got {len(vectors)}")

        self.calls += 1
        self.embedded += len(missing)
        self._cache.update(zip(missing, vectors, strict=True))

    def similarity(self, text: str, reference: str) -> float | None:
        """Cosine between two already-embedded strings, or None."""
        left, right = self._cache.get(text), self._cache.get(reference)
        if left is None or right is None:
            return None
        return cosine(left, right)


def _text_for(record: dict[str, Any], field_name: str) -> str:
    """The value to embed for one rule.

    A rule can name ``*`` to mean the whole record, which is what a filter
    like "anything about laptops" wants - the topic can be in the title on one
    site and in a summary on the next.
    """
    if field_name == "*":
        parts = [str(v) for v in record.values() if isinstance(v, str | int | float)]
        return " ".join(parts)[:MAX_TEXT_CHARS]

    value = record.get(field_name)
    if value is None:
        return ""
    return str(value)[:MAX_TEXT_CHARS]


async def apply_all_filters(
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    filters: tuple[RecordFilter, ...] | list[RecordFilter],
    *,
    scorer: SemanticScorer | None = None,
) -> FilterResult:
    """Deterministic rules, then the semantic ones on what survived.

    With no scorer the semantic rules are skipped rather than failing: a
    recipe carrying one must still run on a machine with no embedding model,
    and dropping every record because a model is missing would be the worst
    of the available answers.
    """
    deterministic = apply_filters(records, filters, deterministic_only=True)
    semantic_rules = [rule for rule in filters if not rule.is_deterministic]

    if not semantic_rules or scorer is None or not deterministic.kept:
        return deterministic

    # One batch for every string involved, references included: they are
    # embedded with the same model and the cache makes repeats free.
    texts: list[str] = [str(rule.value) for rule in semantic_rules]
    for record in deterministic.kept:
        texts.extend(_text_for(record, rule.field) for rule in semantic_rules)
    await scorer.embed_all([t for t in texts if t])

    kept: list[dict[str, Any]] = []
    reasons = dict(deterministic.reasons)

    for record in deterministic.kept:
        for rule in semantic_rules:
            text = _text_for(record, rule.field)
            score = scorer.similarity(text, str(rule.value)) if text else None
            if score is None or score < rule.threshold:
                key = f"{rule.field} semantic"
                reasons[key] = reasons.get(key, 0) + 1
                break
        else:
            kept.append(record)

    return FilterResult(
        kept=kept,
        dropped=len(records) - len(kept),
        reasons=reasons,
    )


@dataclass(slots=True)
class RecordSieve:
    """A recipe's ``required`` fields and ``filters``, applied to a crawl.

    This exists because they were not being applied. ``recipe test`` honoured
    both and a real crawl ignored both entirely - so a recipe that kept three
    Einstein quotes under test collected all ten on the crawl, and nothing
    said so. The same family of gap as a worker that loaded no recipe: built,
    tested, and reachable from nowhere.

    One sieve rather than logic inside each of the four extractors. The rules
    are about records, and records look the same whichever source produced
    them.
    """

    filters: tuple[RecordFilter, ...] = ()
    required: tuple[str, ...] = ()
    scorer: SemanticScorer | None = None

    @property
    def active(self) -> bool:
        return bool(self.filters or self.required)

    async def __call__(
        self, records: tuple[dict[str, Any], ...]
    ) -> tuple[tuple[dict[str, Any], ...], int, dict[str, int]]:
        """Returns the survivors, how many were dropped, and why."""
        if not records or not self.active:
            return records, 0, {}

        reasons: dict[str, int] = {}
        surviving = list(records)

        if self.required:
            # Not a filter: a record missing its title is not a partial
            # record, it is noise, and counting it would drag the fill rate
            # down for a row that should never have existed.
            kept = [r for r in surviving if all(r.get(n) not in (None, "") for n in self.required)]
            if len(kept) != len(surviving):
                reasons["required"] = len(surviving) - len(kept)
            surviving = kept

        if not self.filters:
            return tuple(surviving), len(records) - len(surviving), reasons

        result = await apply_all_filters(surviving, self.filters, scorer=self.scorer)
        for key, count in result.reasons.items():
            reasons[key] = reasons.get(key, 0) + count
        return tuple(result.kept), len(records) - len(result.kept), reasons
