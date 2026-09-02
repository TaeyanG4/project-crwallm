"""Record filters.

URL filters run before a fetch and save the request. These run after
extraction and decide what is worth keeping - "only videos between one and
thirty minutes", "only products under two million won", "only postings from
this year".

The gap this closes was found by asking a plain question: can it collect only
the videos I want? Everything needed to *find* them existed; nothing existed
to *narrow* them. It is not video-specific - products, articles and job
postings all need it.

**Operators are a closed set.** A filter is data, and data that evaluates
arbitrary expressions is a payload, not data (docs/17_NON_GOALS.md). Every
operator below is total: given a value it cannot interpret it drops the record
rather than raising, because one odd row must not end a crawl.

``semantic`` is declared here and evaluated in Phase 4, where the embedding
model lives. It appears now so the schema does not change under existing
recipes when it arrives.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["FilterOp", "FilterResult", "RecordFilter", "apply_filters"]

type FilterOp = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "in",
    "not_in",
    "matches",
    "not_matches",
    "contains",
    "not_contains",
    "exists",
    "missing",
    "semantic",
]

_NEEDS_VALUE = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "in",
        "not_in",
        "matches",
        "not_matches",
        "contains",
        "not_contains",
        "semantic",
    }
)

_DETERMINISTIC = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "in",
        "not_in",
        "matches",
        "not_matches",
        "contains",
        "not_contains",
        "exists",
        "missing",
    }
)


class RecordFilter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    op: FilterOp
    value: Any = None
    threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    """``semantic`` only: cosine similarity below this drops the record."""

    @model_validator(mode="after")
    def _value_present_when_required(self) -> RecordFilter:
        if self.op in _NEEDS_VALUE and self.value is None:
            raise ValueError(f"operator {self.op!r} needs a value")
        if self.op == "between" and (
            not isinstance(self.value, list | tuple) or len(self.value) != 2
        ):
            raise ValueError("between needs exactly two values")
        if self.op in ("matches", "not_matches"):
            try:
                re.compile(str(self.value))
            except re.error as exc:
                raise ValueError(f"invalid regex {self.value!r}: {exc}") from exc
        return self

    @property
    def is_deterministic(self) -> bool:
        """False for ``semantic``, which needs a model.

        The split matters at runtime: cheap filters run first so the expensive
        one sees as few records as possible
        (docs/06_EXTRACTION_ARCHITECTURE.md).
        """
        return self.op in _DETERMINISTIC


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        from crwallm.crawler.extraction.transforms import apply_chain

        parsed = apply_chain(value, ["to_number"])
        return float(parsed) if isinstance(parsed, int | float) else None
    return None


def _as_comparable(value: Any) -> Any:
    """Coerce to something orderable.

    Dates arrive as ISO strings because records are JSONB, so a naive ``>``
    would compare them lexically. That happens to work for ISO-8601 and stops
    working the moment a timezone offset appears.
    """
    if isinstance(value, datetime | date):
        return value
    number = _as_number(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


def _compare(left: Any, right: Any) -> int | None:
    """``-1``/``0``/``1``, or ``None`` when the two are not comparable."""
    a, b = _as_comparable(left), _as_comparable(right)
    # Comparing an aware datetime with a naive one raises, and both spellings
    # turn up: a page's own timestamps are usually naive, a filter written by
    # hand is usually not.
    if (
        isinstance(a, datetime)
        and isinstance(b, datetime)
        and (a.tzinfo is None or b.tzinfo is None)
    ):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    try:
        if a < b:
            return -1
        if a > b:
            return 1
    except TypeError:
        return None
    return 0


def _evaluate(record: dict[str, Any], rule: RecordFilter) -> bool:
    present = rule.field in record
    value = record.get(rule.field)

    match rule.op:
        case "exists":
            return present and value is not None and value != ""
        case "missing":
            return not present or value is None or value == ""

    if value is None:
        # A rule about a field that is not there cannot be satisfied. Dropping
        # rather than keeping is the safer default: a filter exists to narrow.
        return False

    match rule.op:
        case "eq":
            return bool(value == rule.value)
        case "ne":
            return bool(value != rule.value)
        case "in":
            return value in _as_sequence(rule.value)
        case "not_in":
            return value not in _as_sequence(rule.value)
        case "contains":
            return str(rule.value) in str(value)
        case "not_contains":
            return str(rule.value) not in str(value)
        case "matches":
            return re.search(str(rule.value), str(value)) is not None
        case "not_matches":
            return re.search(str(rule.value), str(value)) is None
        case "between":
            low, high = rule.value
            lo, hi = _compare(value, low), _compare(value, high)
            return lo is not None and hi is not None and lo >= 0 and hi <= 0
        case "gt" | "gte" | "lt" | "lte":
            result = _compare(value, rule.value)
            if result is None:
                return False
            return {
                "gt": result > 0,
                "gte": result >= 0,
                "lt": result < 0,
                "lte": result <= 0,
            }[rule.op]
        case _:
            # "semantic", which is evaluated in Phase 4 where the embedding
            # model lives. Passing rather than dropping means a recipe written
            # against that capability still collects today, instead of
            # silently returning nothing.
            return True


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, list | tuple | set | frozenset):
        return tuple(value)
    return (value,)


class FilterResult(BaseModel):
    kept: list[dict[str, Any]]
    dropped: int
    reasons: dict[str, int]
    """Which rule dropped how many. A filter that removes everything is a
    common mistake, and the count is what says which one did it."""


def apply_filters(
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    filters: tuple[RecordFilter, ...] | list[RecordFilter],
    *,
    deterministic_only: bool = True,
) -> FilterResult:
    """Apply ``filters`` to ``records``.

    ``deterministic_only`` skips the rules that need a model, which is the
    Phase 3 behaviour and remains the behaviour when no model is configured.
    """
    active = [f for f in filters if f.is_deterministic or not deterministic_only]
    if not active:
        return FilterResult(kept=list(records), dropped=0, reasons={})

    kept: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}

    for record in records:
        for rule in active:
            if not _evaluate(record, rule):
                key = f"{rule.field} {rule.op}"
                reasons[key] = reasons.get(key, 0) + 1
                break
        else:
            kept.append(record)

    return FilterResult(kept=kept, dropped=len(records) - len(kept), reasons=reasons)
