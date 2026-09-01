"""The engine's collaborators, as protocols.

The engine takes these as arguments rather than importing implementations, so
core stays free of httpx, Playwright and PostgreSQL
(docs/03_SYSTEM_ARCHITECTURE.md). It also means the engine can be driven
entirely by fakes in tests, with no network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from crwallm.policy.url import NormalizedUrl
from crwallm.schemas.types import ErrorKind, FetchMode


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: NormalizedUrl
    depth: int
    mode: FetchMode
    timeout_s: float
    byte_limit: int


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """What came back. ``body`` is bytes because the encoding is the
    extractor's problem, not the fetcher's."""

    url: NormalizedUrl
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    fetch_mode: FetchMode
    redirects: int = 0
    final_url: str | None = None
    from_cache: bool = False

    @property
    def content_type(self) -> str | None:
        raw = self.headers.get("content-type")
        return raw.split(";", 1)[0].strip().lower() if raw else None


@dataclass(frozen=True, slots=True)
class FetchFailure:
    url: NormalizedUrl
    error_kind: ErrorKind
    message: str
    retryable: bool = False
    status: int | None = None
    retry_after_s: float | None = None
    """From a ``Retry-After`` header. Feeds the adaptive controller."""


FetchOutcome = FetchResponse | FetchFailure


class Fetcher(Protocol):
    """Retrieves one URL. Implementations: HTTP (Phase 2), browser (Phase 7)."""

    async def fetch(self, request: FetchRequest) -> FetchOutcome: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FrontierItem:
    url: NormalizedUrl
    depth: int
    discovered_from: str | None = None
    priority: int = 0


class Frontier(Protocol):
    """The queue of URLs still to visit.

    Implementations: in-memory BFS (Phase 2), host-partitioned priority queue
    backed by PostgreSQL (Phase 5). The engine only ever sees this interface,
    so the upgrade is a substitution.
    """

    async def add(self, item: FrontierItem) -> bool:
        """Enqueue. ``False`` when the URL was already known."""
        ...

    async def next(self) -> FrontierItem | None:
        """Claim the next item, or ``None`` when nothing is available now."""
        ...

    async def done(self, item: FrontierItem) -> None: ...

    @property
    def pending(self) -> int: ...

    @property
    def seen(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    extractor: str
    records: tuple[dict[str, Any], ...] = ()
    links: tuple[str, ...] = ()
    canonical_url: str | None = None
    text: str | None = None
    content_hash: str | None = None


class Extractor(Protocol):
    """Turns a response into records and links.

    A pipeline of these runs structured -> semi-structured -> unstructured and
    stops at the first success (docs/06_EXTRACTION_ARCHITECTURE.md).
    """

    name: str

    def supports(self, response: FetchResponse) -> bool: ...

    def extract(self, response: FetchResponse) -> ExtractionResult: ...


@dataclass(slots=True)
class CrawlStats:
    """Running totals the engine reports through ``Progress`` events."""

    pages_fetched: int = 0
    pages_failed: int = 0
    records_extracted: int = 0
    records_filtered: int = 0
    urls_rejected: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)

    def note_rejection(self, reason: str) -> None:
        self.urls_rejected[reason] = self.urls_rejected.get(reason, 0) + 1

    def note_error(self, kind: str) -> None:
        self.errors[kind] = self.errors.get(kind, 0) + 1


class UrlGate(Protocol):
    """Everything a URL must survive before it is fetched.

    Composed in Phase 2 from scope, SSRF, traps and user filters. Kept as one
    protocol so the engine has a single call site and the ordering (cheapest
    check first) lives in one place.
    """

    async def admit(self, url: NormalizedUrl, depth: int) -> Sequence[str]:
        """Empty sequence to admit; otherwise the rejection reasons."""
        ...
