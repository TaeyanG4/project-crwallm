"""CrawlEvent - the engine's only output channel.

The engine yields these and nothing else; every consumer (the DB sink, SSE,
the CLI progress line, metrics) is built on top. docs/04_CRAWLING_ARCHITECTURE.md

Two rules make this a contract rather than a data class:

**Append-only.** Events are persisted in ``crawl_events``. Adding a member or
an optional field is fine; renaming or removing one is a migration of stored
rows. Decide the shape now, extend it later.

**No job_id.** The engine does not know what a job is - jobs live in the
service layer, the engine lives in core. The sink attaches ``job_id`` when it
writes. Letting the engine carry it would drag persistence into core.

Note the wire/storage split: ``RecordsExtracted`` carries the records inline
because consumers need them, but the sink writes the payloads to
``extracted_records`` and persists only the counts in the event log. Storing
both would duplicate the entire result set.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from crwallm.schemas.types import ErrorKind, FetchMode, RejectReason


def _now() -> datetime:
    return datetime.now(UTC)


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------- lifecycle


class JobStarted(_Event):
    type: Literal["job.started"] = "job.started"
    spec_id: str
    seeds: tuple[str, ...]


class JobCompleted(_Event):
    type: Literal["job.completed"] = "job.completed"
    pages_fetched: int
    records_extracted: int
    elapsed_s: float


class JobFailed(_Event):
    type: Literal["job.failed"] = "job.failed"
    error_kind: ErrorKind
    message: str


class JobCancelled(_Event):
    type: Literal["job.cancelled"] = "job.cancelled"
    pages_fetched: int
    reason: str | None = None


# ------------------------------------------------------------------- page


class PageFetched(_Event):
    type: Literal["page.fetched"] = "page.fetched"
    url: str
    status: int
    content_type: str | None = None
    bytes: int
    elapsed_ms: int
    depth: int
    fetch_mode: FetchMode
    from_cache: bool = False
    redirects: int = 0
    final_url: str | None = None
    """Set when redirects moved the fetch somewhere other than ``url``."""


class PageFailed(_Event):
    type: Literal["page.failed"] = "page.failed"
    url: str
    depth: int
    error_kind: ErrorKind
    message: str | None = None
    retryable: bool = False


# --------------------------------------------------------------- discovery


class LinksDiscovered(_Event):
    type: Literal["links.discovered"] = "links.discovered"
    url: str
    found: int
    enqueued: int


class UrlRejected(_Event):
    type: Literal["url.rejected"] = "url.rejected"
    url: str
    reason: RejectReason
    detail: str | None = None


class PatternBudgetExhausted(_Event):
    type: Literal["pattern.budget_exhausted"] = "pattern.budget_exhausted"
    pattern: str
    limit: int


class DuplicateDetected(_Event):
    type: Literal["duplicate.detected"] = "duplicate.detected"
    url: str
    duplicate_of: str
    via: Literal["url", "canonical", "content"]


# ---------------------------------------------------------------- records


class RecordsExtracted(_Event):
    type: Literal["records.extracted"] = "records.extracted"
    url: str
    extractor: str
    count: int
    records: tuple[dict[str, Any], ...] = ()
    """Inline on the wire, counts-only in the event log - see module docstring."""


class RecordsFiltered(_Event):
    type: Literal["records.filtered"] = "records.filtered"
    url: str
    kept: int
    dropped: int
    reasons: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------- progress


class Progress(_Event):
    type: Literal["progress"] = "progress"
    pages_done: int
    pages_queued: int
    records_total: int
    hosts_active: int = 0


type CrawlEvent = Annotated[
    JobStarted
    | JobCompleted
    | JobFailed
    | JobCancelled
    | PageFetched
    | PageFailed
    | LinksDiscovered
    | UrlRejected
    | PatternBudgetExhausted
    | DuplicateDetected
    | RecordsExtracted
    | RecordsFiltered
    | Progress,
    Field(discriminator="type"),
]
"""Discriminated union: consumers can ``match`` exhaustively and mypy will
flag a branch they forgot."""

TERMINAL_EVENTS = (JobCompleted, JobFailed, JobCancelled)
"""A stream ends with exactly one of these. SSE closes on them."""
