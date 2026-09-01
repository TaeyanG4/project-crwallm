"""Crawler-trap defences.

``max_pages`` does not protect a spider. It bounds how much rubbish gets
collected, not whether the budget goes to rubbish: an infinite calendar will
consume five hundred pages as happily as five hundred products.

The guards here are ordered by cost. Everything is a cheap string check except
the pattern budget, which is the one that actually does the work - see
``PatternBudget``. docs/05_SPIDER_ARCHITECTURE.md section 1
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from crwallm.policy.url import NormalizedUrl, url_pattern
from crwallm.schemas.spec import SpiderConfig
from crwallm.schemas.types import RejectReason

__all__ = ["PatternBudget", "TrapGuard", "TrapVerdict"]


@dataclass(frozen=True, slots=True)
class TrapVerdict:
    ok: bool
    reason: RejectReason | None = None
    detail: str | None = None

    @classmethod
    def allow(cls) -> TrapVerdict:
        return cls(True)

    @classmethod
    def deny(cls, reason: RejectReason, detail: str) -> TrapVerdict:
        return cls(False, reason, detail)


@dataclass(slots=True)
class PatternBudget:
    """Per-pattern counters.

    One knob that kills three traps at once::

        /calendar/2031/07 -> /calendar/{n}/{n}   budget 500, calendar dies at 500
        ?page=99999       -> ?page={v}           endless pagination dies too
        ?a=&b=&c=         -> facet combinations  each shape capped separately

    Without it a single generative URL shape eats the whole crawl.
    """

    limit: int
    counts: Counter[str] = field(default_factory=Counter)
    exhausted: set[str] = field(default_factory=set)
    _unreported: set[str] = field(default_factory=set, repr=False)

    def take(self, pattern: str) -> bool:
        """Claim one slot. ``False`` once the pattern is spent."""
        if pattern in self.exhausted:
            return False
        self.counts[pattern] += 1
        if self.counts[pattern] > self.limit:
            self.exhausted.add(pattern)
            self._unreported.add(pattern)
            return False
        return True

    def just_exhausted(self, pattern: str) -> bool:
        """True exactly once per pattern, so the event is emitted once.

        Consuming the flag rather than comparing counters: once a pattern is
        spent ``take`` stops incrementing, so a count-based check would keep
        reporting the transition forever.
        """
        if pattern in self._unreported:
            self._unreported.discard(pattern)
            return True
        return False

    def exhaust(self, pattern: str) -> None:
        """Burn a pattern outright - used when soft-404 detection concludes a
        whole shape is worthless (Phase 5)."""
        if pattern not in self.exhausted:
            self.exhausted.add(pattern)
            self._unreported.add(pattern)


class TrapGuard:
    """Stateful per-crawl guard. Not thread-safe; one per job."""

    def __init__(self, config: SpiderConfig) -> None:
        self._cfg = config
        self.budget = PatternBudget(config.per_pattern_budget)

    def check(self, url: NormalizedUrl) -> TrapVerdict:
        """Cheap structural checks first, budget last."""
        cfg = self._cfg

        if len(url.url) > cfg.max_url_length:
            return TrapVerdict.deny(
                RejectReason.URL_LENGTH,
                f"{len(url.url)} > {cfg.max_url_length}",
            )

        segments = [s for s in url.path.split("/") if s]
        if len(segments) > cfg.max_path_depth:
            return TrapVerdict.deny(
                RejectReason.PATH_DEPTH,
                f"{len(segments)} > {cfg.max_path_depth}",
            )

        repeat = _max_segment_repeat(segments)
        if repeat > cfg.max_repeated_segment:
            return TrapVerdict.deny(
                RejectReason.REPEATED_SEGMENT,
                f"a segment repeats {repeat}x (max {cfg.max_repeated_segment})",
            )

        params = parse_qsl(urlsplit(url.url).query, keep_blank_values=True)
        if len(params) > cfg.max_query_params:
            return TrapVerdict.deny(
                RejectReason.QUERY_PARAMS,
                f"{len(params)} > {cfg.max_query_params}",
            )

        pattern = url_pattern(url)
        if not self.budget.take(pattern):
            return TrapVerdict.deny(
                RejectReason.PATTERN_BUDGET,
                f"pattern {pattern!r} exhausted at {cfg.per_pattern_budget}",
            )

        return TrapVerdict.allow()

    def pattern_of(self, url: NormalizedUrl) -> str:
        return url_pattern(url)


def _max_segment_repeat(segments: list[str]) -> int:
    """How often the most repeated path segment appears.

    Catches ``/a/b/a/b/a/b`` - a symlink loop or a router that happily accepts
    its own prefix. Counting distinct repeats rather than adjacency matters:
    the loop is rarely adjacent.
    """
    if not segments:
        return 0
    return max(Counter(segments).values())
