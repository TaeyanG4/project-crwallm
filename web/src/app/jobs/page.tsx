"use client";

import { useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { duration, relativeTime } from "@/lib/format";
import { StatusChip } from "@/components/StatusChip";
import { NewCrawlForm } from "@/components/NewCrawlForm";
import { usePolled } from "@/hooks/usePolled";
import { TERMINAL_STATUSES, type JobSummary } from "@/lib/types";

/** Only while something is moving. A finished list does not change on its own. */
const POLL_MS = 2000;

export default function JobsPage() {
  const [polling, setPolling] = useState(false);
  const { data, error, loaded, refresh } = usePolled<JobSummary[]>(
    () => api.listJobs(),
    polling ? POLL_MS : null,
    [polling],
  );

  const jobs = data ?? [];
  const anyActive = jobs.some((j) => !TERMINAL_STATUSES.includes(j.status));

  // Adjusted during render rather than in an effect: the poll rate is a pure
  // function of the response just rendered, and React re-renders immediately
  // rather than painting the stale value first.
  if (anyActive !== polling) setPolling(anyActive);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-6 p-6">
        <section className="space-y-3">
          <h1 className="text-lg font-semibold">새 크롤</h1>
          <NewCrawlForm onSubmitted={refresh} />
        </section>

        <section className="space-y-3">
          <div className="flex items-baseline justify-between">
            <h2 className="text-lg font-semibold">최근 실행</h2>
            {anyActive && <span className="text-xs text-muted-foreground">자동 새로고침 중</span>}
          </div>

          {error && <p className="rounded-md bg-bad/10 px-3 py-2 text-sm text-bad">{error}</p>}

          {loaded && jobs.length === 0 && !error && (
            <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
              아직 실행한 크롤이 없습니다.
            </p>
          )}

          <div className="divide-y overflow-hidden rounded-lg border">
            {jobs.map((job) => (
              <Link
                key={job.id}
                href={`/jobs/${job.id}`}
                className="flex items-center gap-4 px-4 py-3 transition-colors hover:bg-muted/50"
              >
                <StatusChip status={job.status} />

                <span className="flex-1 truncate font-mono text-xs text-muted-foreground">
                  {job.id.slice(0, 8)}
                </span>

                <span className="tabular-nums text-sm">
                  {job.pages_crawled}
                  <span className="ml-1 text-xs text-muted-foreground">페이지</span>
                </span>

                {/* Zero records on a finished crawl is the single most common
                    thing to go wrong, so it is coloured rather than left to
                    blend in with the other numbers. */}
                <span
                  className={
                    job.records_extracted === 0 && job.status === "completed"
                      ? "tabular-nums text-sm text-warn"
                      : "tabular-nums text-sm"
                  }
                >
                  {job.records_extracted}
                  <span className="ml-1 text-xs text-muted-foreground">레코드</span>
                </span>

                {job.pages_failed > 0 && (
                  <span className="tabular-nums text-sm text-bad">
                    {job.pages_failed}
                    <span className="ml-1 text-xs">실패</span>
                  </span>
                )}

                <span className="w-16 text-right text-xs text-muted-foreground">
                  {duration(job.started_at, job.completed_at)}
                </span>

                <span className="w-20 text-right text-xs text-muted-foreground">
                  {relativeTime(job.created_at)}
                </span>
              </Link>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
