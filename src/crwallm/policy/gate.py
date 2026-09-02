"""The single place a URL is admitted or refused.

Phase 1 built the predicates; this composes them, and the composition is the
design decision.

**Two stages, each run exactly once per URL.**

``check_enqueue`` decides whether a URL is worth queueing. Everything it does
is local: scope, user filters, depth, trap guards. No network. This is where
the pattern budget is claimed, because enqueueing is the moment we commit to
eventually fetching - claiming it later would let the queue grow without
bound, and claiming it twice would halve every budget.

``admit_fetch`` runs immediately before the request and does only what has to
happen at that instant: the page budget, and SSRF with pinning. SSRF is last
because it is the only step that costs a DNS round trip, and on a spider most
discovered links are rejected long before that point - paying to resolve them
first would dominate the crawl.

Deduplication is deliberately *not* here. The frontier owns it, because the
frontier is what a URL is added to; two seen-sets would drift.

Verdicts carry a reason rather than a boolean, because those reasons are the
tuning signal: "3000 rejected" says nothing, "2900 scope, 90 pattern_budget"
says the seeds were wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from crwallm.policy.domains import DomainScope
from crwallm.policy.ssrf import PinnedTarget, SsrfBlockedError, SsrfGuard
from crwallm.policy.traps import TrapGuard
from crwallm.policy.url import NormalizedUrl
from crwallm.schemas.spec import CrawlSpec
from crwallm.schemas.types import RejectReason

__all__ = ["GateVerdict", "Scope", "UrlGate"]


class Scope(Protocol):
    """Is this host inside the crawl?

    A protocol rather than ``DomainScope`` outright because the scope is
    something callers legitimately compute for themselves: a recipe reuse
    intersects two scopes (docs/07_RECIPE_ARCHITECTURE.md), and a crawl
    aimed at a bare address has no registrable domain to speak of.
    """

    def contains(self, host: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class GateVerdict:
    admitted: bool
    reason: RejectReason | None = None
    detail: str | None = None
    target: PinnedTarget | None = None
    """Present only after ``admit_fetch`` succeeds - it is what the fetcher
    connects to."""

    @classmethod
    def allow(cls, target: PinnedTarget | None = None) -> GateVerdict:
        return cls(True, target=target)

    @classmethod
    def deny(cls, reason: RejectReason, detail: str = "") -> GateVerdict:
        return cls(False, reason, detail or None)


@dataclass(slots=True)
class UrlGate:
    """Stateful per-crawl gate.

    Holds the trap budgets and the page counter, so one instance belongs to one
    job and is never shared between them.
    """

    spec: CrawlSpec
    scope: Scope
    traps: TrapGuard
    ssrf: SsrfGuard
    _include: tuple[re.Pattern[str], ...] = field(default_factory=tuple)
    _exclude: tuple[re.Pattern[str], ...] = field(default_factory=tuple)
    _pages_admitted: int = 0

    @classmethod
    def build(cls, spec: CrawlSpec, ssrf: SsrfGuard, *, scope: Scope | None = None) -> UrlGate:
        """Assemble the gate for one crawl.

        ``scope`` defaults to the spec's ``allowed_domains`` run through the
        PSL. Pass one explicitly when it has already been decided elsewhere -
        a recipe intersection, or a target with no registrable domain.
        """
        return cls(
            spec=spec,
            scope=scope if scope is not None else DomainScope.from_spec(spec.allowed_domains),
            traps=TrapGuard(spec.spider),
            ssrf=ssrf,
            _include=tuple(re.compile(p) for p in spec.url_filters.include),
            _exclude=tuple(re.compile(p) for p in spec.url_filters.exclude),
        )

    def check_enqueue(self, url: NormalizedUrl, depth: int) -> GateVerdict:
        """Is this URL worth a slot in the frontier?

        Local checks only. Claims the pattern budget on success, so it must be
        called once per URL - at the point of enqueueing, and nowhere else.
        """
        if depth > self.spec.limits.max_depth:
            return GateVerdict.deny(RejectReason.DEPTH, f"{depth} > {self.spec.limits.max_depth}")

        if not self.scope.contains(url.host):
            return GateVerdict.deny(RejectReason.SCOPE, url.host)

        if self._exclude and any(p.search(url.url) for p in self._exclude):
            return GateVerdict.deny(RejectReason.URL_FILTER, "matched exclude")

        if self._include and not any(p.search(url.url) for p in self._include):
            return GateVerdict.deny(RejectReason.URL_FILTER, "matched no include")

        # Claims budget. Last, so a URL rejected for scope does not spend one.
        verdict = self.traps.check(url)
        if not verdict.ok:
            assert verdict.reason is not None
            return GateVerdict.deny(verdict.reason, verdict.detail or "")

        return GateVerdict.allow()

    async def admit_fetch(self, url: NormalizedUrl) -> GateVerdict:
        """Final gate, run immediately before the request goes out.

        SSRF lives here rather than at enqueue time for two reasons: it costs
        DNS, and its result - the pinned address - is only valid for a fetch
        happening now.
        """
        if self._pages_admitted >= self.spec.limits.max_pages:
            return GateVerdict.deny(RejectReason.MAX_PAGES, str(self.spec.limits.max_pages))

        try:
            target = await self.ssrf.check(url)
        except SsrfBlockedError as exc:
            return GateVerdict.deny(RejectReason.SSRF, exc.reason)

        self._pages_admitted += 1
        return GateVerdict.allow(target)

    @property
    def pages_admitted(self) -> int:
        return self._pages_admitted

    @property
    def budget_exhausted(self) -> bool:
        return self._pages_admitted >= self.spec.limits.max_pages
