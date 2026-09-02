"""Getting the data out.

A crawl that can only be read through its own API is a crawl whose results
live in this tool forever. Everything here streams: a job with half a million
records must not be assembled in memory to be downloaded, on either side.

**CSV needs to know its columns before it writes a row, and the rows are
JSONB.** Scanning them in Python would mean holding the lot; asking Postgres
for the distinct keys is one index-free but cheap pass that returns a few
dozen strings. That is why the column discovery is a query rather than a loop.

Parquet is deliberately absent. It would mean pyarrow - forty megabytes and a
build toolchain - for a format nothing in a local workflow opens. JSONL feeds
every tool that reads JSON, CSV opens in a spreadsheet, and either can be
turned into Parquet by whatever is going to read it
(docs/17_NON_GOALS.md).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from crwallm.db.models import ExtractedRecord

__all__ = ["EXPORT_FORMATS", "ExportFormat", "content_type_for", "export_records", "filename_for"]

type ExportFormat = Literal["jsonl", "csv"]

EXPORT_FORMATS: tuple[str, ...] = ("jsonl", "csv")

CHUNK_ROWS = 500
"""Rows fetched per round trip.

Large enough that the per-query cost disappears, small enough that one chunk
is a few hundred kilobytes rather than the whole result set."""


def content_type_for(fmt: str) -> str:
    return "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson"


def filename_for(job_id: UUID, fmt: str) -> str:
    return f"crwallm-{str(job_id)[:8]}.{fmt}"


async def _column_names(session: AsyncSession, job_id: UUID) -> list[str]:
    """Every key present across a job's records, in a stable order.

    ``jsonb_object_keys`` over the whole set rather than a sample: a recipe
    whose last field only appears on the final page would otherwise produce a
    file missing a column, which is worse than a slow header - it is a file
    that looks complete.
    """
    result = await session.execute(
        text(
            "SELECT DISTINCT jsonb_object_keys(data) AS key "
            "FROM extracted_records WHERE job_id = :job_id"
        ),
        {"job_id": job_id},
    )
    return sorted(row.key for row in result)


async def _rows(session: AsyncSession, job_id: UUID) -> AsyncIterator[ExtractedRecord]:
    """Records in the order they were extracted, a chunk at a time.

    Keyset paging on ``seq`` rather than OFFSET: an offset scan re-reads every
    skipped row, so exporting the last chunk of a large job would cost a full
    scan per chunk.

    ``seq`` rather than ``(created_at, id)``, which was the first attempt and
    shuffled the output. The sink writes in batches and Postgres's ``now()``
    is the transaction's start time, so a whole batch shares a timestamp and
    the tiebreaker becomes a random UUID.
    """
    cursor = 0

    while True:
        chunk = list(
            (
                await session.execute(
                    select(ExtractedRecord)
                    .where(ExtractedRecord.job_id == job_id, ExtractedRecord.seq > cursor)
                    .order_by(ExtractedRecord.seq)
                    .limit(CHUNK_ROWS)
                )
            ).scalars()
        )
        if not chunk:
            return

        for row in chunk:
            yield row
        cursor = chunk[-1].seq


async def export_records(
    session: AsyncSession,
    job_id: UUID,
    fmt: str = "jsonl",
    *,
    include_source: bool = False,
) -> AsyncIterator[str]:
    """Stream one job's records in ``fmt``.

    ``include_source`` adds the page each row came from and the extractor that
    read it. Off by default and appended rather than merged: a recipe can have
    a field called ``page_url`` of its own, and silently overwriting it would
    corrupt the export in a way nothing downstream could detect.
    """
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"unknown export format {fmt!r}; expected one of {list(EXPORT_FORMATS)}")

    if fmt == "jsonl":
        async for row in _rows(session, job_id):
            payload: dict[str, Any] = dict(row.data)
            if include_source:
                payload["_page_url"] = row.page_url
                payload["_extractor"] = row.extractor
            yield json.dumps(payload, ensure_ascii=False) + "\n"
        return

    columns = await _column_names(session, job_id)
    if include_source:
        columns += ["_page_url", "_extractor"]

    buffer = io.StringIO()
    # `\n` rather than the module default of `\r\n`: these files are read on
    # the machine that wrote them far more often than they are mailed to
    # Windows, and a stray carriage return in a JSON-derived value is a
    # nuisance either way.
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    yield _drain(buffer)

    async for row in _rows(session, job_id):
        record = {key: _flatten(value) for key, value in row.data.items()}
        if include_source:
            record["_page_url"] = row.page_url
            record["_extractor"] = row.extractor
        writer.writerow(record)
        yield _drain(buffer)


def _drain(buffer: io.StringIO) -> str:
    value = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return value


def _flatten(value: Any) -> str:
    """One cell.

    A CSV cell is a string, and a record's value can be a list - tags, several
    authors. Serialising those as JSON keeps them readable and reversible;
    ``str()`` on a Python list would emit single quotes that no JSON parser
    accepts back.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
