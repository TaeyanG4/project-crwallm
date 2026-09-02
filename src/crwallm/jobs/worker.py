"""The crawl worker.

A separate process from the API, deliberately. A five-hundred page crawl runs
for minutes; putting it inside a request would block a connection for the
duration, and moving it out afterwards would mean rewriting the router, the
service layer and the persistence path at once (docs/09_JOB_ARCHITECTURE.md).

Runs in the Linux container so uvloop applies - it is worth two to four times
on high-concurrency socket work, and it does not exist on Windows
(docs/12_PERFORMANCE.md).

Phase 2 is a polling loop: claim, crawl, persist, repeat. Cancellation,
retries and stale recovery arrive in Phase 8; the rows they need are already
in place.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid
from pathlib import Path

from crwallm.crawler.adapters import drain_to
from crwallm.db.session import dispose_engine, get_sessionmaker
from crwallm.jobs.sink import PostgresEventSink
from crwallm.schemas.types import ErrorKind
from crwallm.services.crawl import RecipeNotApplicableError, open_crawl, resolve_plan
from crwallm.services.job import JobService

__all__ = ["Worker", "main"]

log = logging.getLogger(__name__)

IDLE_POLL_S = 1.0
"""Sleep between empty polls. Long enough not to hammer the database, short
enough that a submitted job starts promptly."""


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Worker:
    def __init__(
        self,
        *,
        archive_dir: Path | None = None,
        recipes_dir: Path | None = None,
        poll_s: float = IDLE_POLL_S,
    ) -> None:
        self.id = _worker_id()
        self._archive_dir = archive_dir
        self._recipes_dir = recipes_dir
        self._poll_s = poll_s
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        """Finish the job in hand, then exit.

        Killing mid-crawl would leave the job in ``running`` with partial
        results, which is exactly the state Phase 8 has to clean up. Draining
        avoids creating the mess in the first place.
        """
        log.info("stop requested; finishing the current job")
        self._stopping.set()

    async def run_forever(self) -> None:
        log.info("worker %s started", self.id)
        try:
            while not self._stopping.is_set():
                claimed = await self.run_once()
                if not claimed and not self._stopping.is_set():
                    await self._sleep_or_stop(self._poll_s)
        finally:
            await dispose_engine()
            log.info("worker %s stopped", self.id)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Idle, but wake immediately on a stop request."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def run_once(self) -> bool:
        """Claim and run one job. ``False`` when the queue was empty."""
        sessionmaker = get_sessionmaker()

        async with sessionmaker() as session:
            claimed = await JobService(session).claim(self.id)
            if claimed is None:
                return False
            job, spec = claimed

        log.info("job %s claimed: %d seed(s)", job.id, len(spec.seed_urls))

        async with sessionmaker() as session:
            jobs = JobService(session)
            sink = PostgresEventSink(session=session, job_id=job.id)
            try:
                # Resolved here rather than at claim time: a recipe that has
                # been edited or deleted since the job was queued should fail
                # this job with a reason, not crash the claim loop.
                plan = resolve_plan(spec, recipes_dir=self._recipes_dir)
            except RecipeNotApplicableError as exc:
                log.warning("job %s rejected: %s", job.id, exc)
                await jobs.mark_failed(job.id, ErrorKind.CONFIG.value, str(exc))
                return True

            try:
                async with open_crawl(plan, archive_dir=self._archive_dir) as events:
                    await drain_to(events, sink)
            except Exception as exc:
                # A job that dies must not take the worker with it, and must
                # not be left looking like it is still running.
                log.exception("job %s failed", job.id)
                await session.rollback()
                await jobs.mark_failed(
                    job.id, ErrorKind.INTERNAL.value, f"{type(exc).__name__}: {exc}"
                )
                return True

        log.info(
            "job %s finished: %d pages, %d records",
            job.id,
            sink.pages_crawled,
            sink.records_extracted,
        )
        return True


def main() -> None:
    from crwallm.config import get_settings
    from crwallm.main import install_uvloop

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    loop_name = install_uvloop()
    log.info("event loop: %s", loop_name)

    worker = Worker(archive_dir=settings.archive_dir, recipes_dir=settings.recipes_dir)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                # Windows has no signal handlers on the proactor loop; there
                # KeyboardInterrupt is the stop path.
                loop.add_signal_handler(sig, worker.request_stop)
        await worker.run_forever()

    # Ctrl-C is the stop path on Windows, where the proactor loop has no
    # signal handlers.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
