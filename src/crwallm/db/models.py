"""Persistence.

Shapes follow docs/10_DATA_MODEL.md. Two decisions carried over from the
design and worth restating where the columns live:

**No tenant column.** This is a local single-user tool
(docs/00_INDEX.md). Adding ``workspace_id`` "just in case" would put a
predicate on every query for a feature that is an explicit non-goal.

**Indexes are sparse on the write-heavy tables.** ``crawl_results`` and
``extracted_records`` are filled by COPY at a few thousand rows a second;
every index is paid on each of those rows. The uniqueness constraints below
are the ones that buy something - idempotent resume, and duplicate collapse -
and the rest wait until a real query asks for them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from crwallm.db.base import Base

__all__ = [
    "CrawlEventRow",
    "CrawlJob",
    "CrawlResult",
    "CrawlSpecRow",
    "ExtractedRecord",
    "JobStatus",
]


class JobStatus:
    """Phase 2's state machine.

    ``retry_wait`` and ``waiting_for_user`` arrive with retries (Phase 8) and
    authenticated interaction (Phase 9). Adding them now would mean writing
    transitions nothing can produce.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = (QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED)
    TERMINAL = (COMPLETED, FAILED, CANCELLED)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class CrawlSpecRow(Base):
    """A stored ``CrawlSpec``.

    The spec is kept as JSONB rather than exploded into columns. It is a
    validated Pydantic document that the engine reads whole, it gains fields
    every phase, and nothing queries its interior - three reasons a column per
    field would be churn without benefit.
    """

    __tablename__ = "crawl_specs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    jobs: Mapped[list[CrawlJob]] = relationship(back_populates="spec")


class CrawlJob(Base):
    """One execution.

    ``worker_id`` and ``heartbeat_at`` exist from the start even though nothing
    reads them until Phase 8: a worker that dies mid-crawl leaves a row stuck
    in ``running``, and recovering it later needs to know who claimed it and
    when they were last alive. Adding the columns afterwards means migrating
    rows that are already stuck.
    """

    __tablename__ = "crawl_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="status_known",
        ),
        # The claim query: queued rows, highest priority first, oldest first.
        Index("ix_crawl_jobs_claim", "status", "priority", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    spec_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crawl_specs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    worker_id: Mapped[str | None] = mapped_column(String(100))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pages_crawled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_extracted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error_kind: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)

    error_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    """The failure taxonomy, tallied. "400 failed" is not actionable; "380 of
    them blocked_429" says to lower concurrency. docs/09_JOB_ARCHITECTURE.md"""

    reject_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    """Why URLs never became fetches. Mostly ``scope`` on a well-aimed crawl;
    a lot of ``pattern_budget`` means a trap was found."""

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    spec: Mapped[CrawlSpecRow] = relationship(back_populates="jobs")


class CrawlResult(Base):
    """One page's fetch outcome.

    ``body_ref`` points into the content-addressed archive rather than holding
    the body: two URLs serving identical bytes cost one blob, and the row stays
    small enough to scan.
    """

    __tablename__ = "crawl_results"
    __table_args__ = (Index("ix_crawl_results_job", "job_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(120))
    content_length: Mapped[int | None] = mapped_column(Integer)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fetch_mode: Mapped[str] = mapped_column(String(10), default="http", nullable=False)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    redirects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    body_ref: Mapped[str | None] = mapped_column(String(64))
    """sha256 of the archived body. Null when nothing was archived."""

    error_kind: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExtractedRecord(Base):
    """One extracted row.

    The unique constraint is what makes resume safe (Phase 8): re-running a
    page that was already processed cannot duplicate its rows, so a crash does
    not have to be reasoned about.
    """

    __tablename__ = "extracted_records"
    __table_args__ = (
        UniqueConstraint("job_id", "page_url", "record_hash", name="record_identity"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="CASCADE"), nullable=False
    )
    page_url: Mapped[str] = mapped_column(Text, nullable=False)
    extractor: Mapped[str] = mapped_column(String(40), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CrawlEventRow(Base):
    """The event log.

    ``BIGSERIAL`` rather than a UUID because the id is the SSE cursor: a
    consumer reconnecting with ``Last-Event-ID`` needs an ordering, and a
    random id has none.

    Payloads are stored trimmed. ``records.extracted`` carries its rows inline
    on the wire because consumers want them, but persisting them here as well
    would store the entire result set twice.
    """

    __tablename__ = "crawl_events"
    __table_args__ = (Index("ix_crawl_events_job_id", "job_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
