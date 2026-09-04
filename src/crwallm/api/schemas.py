"""Request and response shapes for the REST surface.

Separate from the ORM on purpose. Returning a ``CrawlJob`` row directly would
make every column a public API detail, so adding an index or renaming a field
would become a breaking change to something the database never intended to
publish.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from crwallm.crawler.extraction.css import CssSpec
from crwallm.schemas.spec import CrawlSpec

__all__ = [
    "JobDetail",
    "JobEvent",
    "JobSubmitRequest",
    "JobSubmitted",
    "JobSummary",
    "PageRow",
    "PageRowList",
    "RecipeDetail",
    "RecipeSummary",
    "RecordPage",
    "RecordSource",
]


class JobSubmitRequest(BaseModel):
    """A crawl to queue.

    The spec is inline rather than referenced. ``POST /api/specs`` exists in
    the plan and arrives with recipes in Phase 3, when a spec is worth naming
    and reusing; until then a separate round trip would buy nothing.
    """

    model_config = ConfigDict(extra="forbid")

    spec: CrawlSpec
    extraction: CssSpec | None = None
    """How to extract. Moves into ``Recipe`` in Phase 3, which owns "how" while
    the spec owns "what" (docs/07_RECIPE_ARCHITECTURE.md)."""

    priority: int = Field(default=0, ge=-100, le=100)


class JobSubmitted(BaseModel):
    """The immediate answer.

    A crawl runs for minutes; the response cannot wait for it
    (docs/09_JOB_ARCHITECTURE.md).
    """

    id: UUID
    status: str
    created_at: datetime


class JobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    pages_crawled: int
    pages_failed: int
    records_extracted: int
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class JobDetail(JobSummary):
    worker_id: str | None = None
    attempts: int = 0
    cancel_requested_at: datetime | None = None
    heartbeat_at: datetime | None = None
    error_kind: str | None = None
    error_message: str | None = None

    error_counts: dict[str, int] = Field(default_factory=dict)
    """Failures by kind. "400 failed" is not actionable; "380 blocked_429"
    says to lower concurrency."""

    reject_counts: dict[str, int] = Field(default_factory=dict)
    """URLs that never became fetches, by reason. Mostly ``scope`` on a
    well-aimed crawl; a lot of ``pattern_budget`` means a trap was found."""


class RecordSource(BaseModel):
    """Which page and which extractor produced one record."""

    model_config = ConfigDict(from_attributes=True)

    page_url: str
    extractor: str


class RecordPage(BaseModel):
    """A page of extracted rows.

    Offset paging is enough while results are read by a human or a script that
    just wants the lot. Keyset paging arrives if a result set ever gets big
    enough for the offset scan to matter.
    """

    job_id: UUID
    total_returned: int
    offset: int
    limit: int
    records: list[dict[str, Any]]

    provenance: list[RecordSource] = Field(default_factory=list)
    """Where each row came from, positionally aligned with ``records``.

    Alongside rather than merged into the row: a record's fields are named by
    its recipe, and quietly adding ``page_url`` to them would collide with a
    site that has a column of that name and change what a downstream consumer
    sees. Kept out of ``records`` so the data stays exactly what was
    extracted.

    Worth surfacing now that a row can come from seven different places. "The
    price is wrong on this row" is answered by knowing whether it was read
    from a selector, from JSON-LD or from microdata."""


class JobEvent(BaseModel):
    """One row of a job's event log.

    ``id`` is a monotonic integer rather than a UUID because it doubles as the
    SSE cursor: a browser that reconnects sends the last id it saw and expects
    the stream to resume after it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class PageRow(BaseModel):
    """A page the crawl actually fetched.

    Distinct from an extracted record: a crawl can fetch two hundred pages and
    extract from twelve, and when that happens the operator needs to see the
    other one hundred and eighty-eight to find out why.
    """

    model_config = ConfigDict(from_attributes=True)

    url: str
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    depth: int
    elapsed_ms: int | None = None
    error_kind: str | None = None
    error_message: str | None = None
    created_at: datetime


class PageRowList(BaseModel):
    job_id: UUID
    total_returned: int
    offset: int
    limit: int
    pages: list[PageRow]


class RecipeSummary(BaseModel):
    """A recipe as the UI lists it.

    The quality numbers travel with the summary because "active" on its own is
    an assertion; "active, 10 records, 100% fill" is the evidence for it, and
    a list that hides the evidence invites trusting a stale recipe.
    """

    name: str
    version: int
    status: str
    source: str
    """Which of the seven extractors reads the records - css, jsonld,
    microdata, embedded, feed, table or article. A screen that cannot show it
    cannot explain why a recipe with no CSS selectors works."""

    source_url: str
    allowed_domains: list[str]
    container: str | None = None
    field_names: list[str]
    record_count: int
    mean_fill: float
    measured_at: datetime | None = None


class RecipeDetail(RecipeSummary):
    fields: list[dict[str, Any]]
    fingerprint: str | None = None
    notes: str | None = None
