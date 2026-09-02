"""Persisting a crawl.

The ``EventSink`` the worker drains into. Three things it does that a naive
"insert per event" would not:

**Batching.** Results and records accumulate and go out in bulk. A crawl at a
few hundred pages a second issues a few hundred round trips a second if each
row is its own INSERT, and the database becomes the bottleneck long before the
network does (docs/12_PERFORMANCE.md).

**Trimming.** ``records.extracted`` carries its rows inline so consumers can
use them, but the event log stores counts only - the rows themselves go to
``extracted_records``. Persisting both would store every result twice.

**Tallying.** Failure kinds and rejection reasons are counted as they stream
past and written onto the job. That is what turns "400 pages failed" into
"380 of them were blocked_429", which is the difference between a number and
a decision.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from crwallm.db.models import CrawlEventRow, CrawlJob, CrawlResult, ExtractedRecord, JobStatus
from crwallm.schemas.events import (
    CrawlEvent,
    JobCancelled,
    JobCompleted,
    JobFailed,
    PageFailed,
    PageFetched,
    RecordsExtracted,
    UrlRejected,
)

__all__ = ["DEFAULT_BATCH_SIZE", "PostgresEventSink", "record_hash"]

DEFAULT_BATCH_SIZE = 500
"""Rows per flush. Large enough that the per-statement cost disappears, small
enough that a crash loses seconds rather than minutes of work."""


def record_hash(page_url: str, data: dict[str, Any]) -> str:
    """Stable identity for one extracted row.

    Keyed on the page and the sorted payload, so re-running a page produces the
    same hashes and the unique constraint absorbs the repeat. That is what
    makes resume (Phase 8) safe without a separate bookkeeping table.
    """
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{page_url}\x00{blob}".encode()).hexdigest()


@dataclass(slots=True)
class PostgresEventSink:
    """Writes one crawl's events, results and records."""

    session: AsyncSession
    job_id: UUID
    batch_size: int = DEFAULT_BATCH_SIZE

    _results: list[dict[str, Any]] = field(default_factory=list)
    _records: list[dict[str, Any]] = field(default_factory=list)
    _events: list[dict[str, Any]] = field(default_factory=list)

    pages_crawled: int = 0
    pages_failed: int = 0
    records_extracted: int = 0
    _errors: Counter[str] = field(default_factory=Counter)
    _rejects: Counter[str] = field(default_factory=Counter)
    _terminal: CrawlEvent | None = None

    async def handle(self, event: CrawlEvent) -> None:
        self._events.append(
            {
                "job_id": self.job_id,
                "event_type": event.type,
                "payload": _trim(event),
                "created_at": event.at,
            }
        )

        match event:
            case PageFetched():
                self.pages_crawled += 1
                self._results.append(
                    {
                        "id": uuid4(),
                        "job_id": self.job_id,
                        "url": event.url,
                        "final_url": event.final_url,
                        "status_code": event.status,
                        "content_type": event.content_type,
                        "content_length": event.bytes,
                        "depth": event.depth,
                        "fetch_mode": event.fetch_mode.value,
                        "elapsed_ms": event.elapsed_ms,
                        "redirects": event.redirects,
                        "created_at": event.at,
                    }
                )
            case PageFailed():
                self.pages_failed += 1
                self._errors[event.error_kind.value] += 1
                self._results.append(
                    {
                        "id": uuid4(),
                        "job_id": self.job_id,
                        "url": event.url,
                        "depth": event.depth,
                        "fetch_mode": "http",
                        "error_kind": event.error_kind.value,
                        "error_message": event.message,
                        "created_at": event.at,
                    }
                )
            case RecordsExtracted():
                self.records_extracted += event.count
                for row in event.records:
                    self._records.append(
                        {
                            "id": uuid4(),
                            "job_id": self.job_id,
                            "page_url": event.url,
                            "extractor": event.extractor,
                            "data": row,
                            "record_hash": record_hash(event.url, row),
                            "created_at": event.at,
                        }
                    )
            case UrlRejected():
                self._rejects[event.reason.value] += 1
            case JobCompleted() | JobFailed() | JobCancelled():
                self._terminal = event
            case _:
                pass

        if self._pending >= self.batch_size:
            await self.flush()

    @property
    def _pending(self) -> int:
        return len(self._results) + len(self._records) + len(self._events)

    async def flush(self) -> None:
        """Write what has accumulated, then update the job's tallies."""
        if self._results:
            await self.session.execute(insert(CrawlResult), self._results)
            self._results.clear()

        if self._records:
            # Re-running a page must not duplicate its rows. The unique
            # constraint plus ON CONFLICT DO NOTHING makes the write
            # idempotent, which is what resume will rely on.
            await self.session.execute(
                insert(ExtractedRecord).on_conflict_do_nothing(constraint="record_identity"),
                self._records,
            )
            self._records.clear()

        if self._events:
            await self.session.execute(insert(CrawlEventRow), self._events)
            self._events.clear()

        await self._update_job()
        await self.session.commit()

    async def _update_job(self) -> None:
        values: dict[str, Any] = {
            "pages_crawled": self.pages_crawled,
            "pages_failed": self.pages_failed,
            "records_extracted": self.records_extracted,
            "error_counts": dict(self._errors),
            "reject_counts": dict(self._rejects),
            "heartbeat_at": datetime.now(UTC),
        }

        match self._terminal:
            case JobCompleted():
                values["status"] = JobStatus.COMPLETED
                values["completed_at"] = datetime.now(UTC)
            case JobFailed() as failed:
                values["status"] = JobStatus.FAILED
                values["error_kind"] = failed.error_kind.value
                values["error_message"] = failed.message
                values["completed_at"] = datetime.now(UTC)
            case JobCancelled():
                values["status"] = JobStatus.CANCELLED
                values["completed_at"] = datetime.now(UTC)
            case _:
                pass

        await self.session.execute(
            update(CrawlJob).where(CrawlJob.id == self.job_id).values(**values)
        )


_TRIM_FIELDS = {"records"}
"""Carried on the wire, dropped from the log - the rows have their own table."""


def _trim(event: CrawlEvent) -> dict[str, Any]:
    payload = event.model_dump(mode="json")
    for key in _TRIM_FIELDS:
        payload.pop(key, None)
    payload.pop("at", None)  # stored in its own column
    return payload
