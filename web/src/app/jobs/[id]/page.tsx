"use client";

import { use, useState } from "react";
import Link from "next/link";
import { api, exportUrl } from "@/lib/api";
import { bytes, cn, duration, shortUrl } from "@/lib/format";
import { EventFeed } from "@/components/EventFeed";
import { RecordTable } from "@/components/RecordTable";
import { StatusChip } from "@/components/StatusChip";
import { useJobStream } from "@/hooks/useJobStream";
import { usePolled } from "@/hooks/usePolled";
import { TERMINAL_STATUSES, type JobDetail, type PageRow } from "@/lib/types";

type Tab = "records" | "pages" | "diagnostics";

/** The job row's counters are updated in batches; the feed carries the detail
 *  in between, so this only has to be fast enough to feel alive. */
const POLL_MS = 1500;

export default function JobPage({ params }: PageProps<"/jobs/[id]">) {
  const { id } = use(params);
  const [tab, setTab] = useState<Tab>("records");
  const [polling, setPolling] = useState(true);
  const [busy, setBusy] = useState(false);

  const job = usePolled<JobDetail>(() => api.getJob(id), polling ? POLL_MS : null, [id, polling]);
  const stream = useJobStream(id, true);

  const detail = job.data;
  const finished = detail !== null && TERMINAL_STATUSES.includes(detail.status);
  if (finished === polling) setPolling(!finished);

  // Results are fetched alongside, and stop when the crawl does. The last
  // batch of rows lands in the same transaction as the terminal status, so a
  // final read after `finished` is what makes them appear.
  const records = usePolled(() => api.getRecords(id, 500), polling ? POLL_MS * 2 : null, [
    id,
    polling,
  ]);
  const pages = usePolled(() => api.getPages(id, 500), polling ? POLL_MS * 2 : null, [id, polling]);

  if (job.error && !detail) {
    return (
      <div className="p-6">
        <p className="rounded-md bg-bad/10 px-3 py-2 text-sm text-bad">{job.error}</p>
        <Link href="/jobs" className="mt-3 inline-block text-sm text-running hover:underline">
          ← 목록으로
        </Link>
      </div>
    );
  }

  const rows = records.data?.records ?? [];
  const provenance = records.data?.provenance ?? [];
  const pageRows = pages.data?.pages ?? [];
  const emptyHarvest = finished && detail !== null && detail.records_extracted === 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b px-6 py-3">
        <div className="mx-auto flex max-w-7xl items-center gap-4">
          <Link href="/jobs" className="text-sm text-muted-foreground hover:text-foreground">
            ←
          </Link>
          {detail && <StatusChip status={detail.status} />}
          <span className="font-mono text-xs text-muted-foreground">{id.slice(0, 8)}</span>

          <div className="ml-auto flex items-center gap-5 text-sm tabular-nums">
            <Metric label="페이지" value={detail?.pages_crawled ?? 0} />
            <Metric
              label="레코드"
              value={detail?.records_extracted ?? 0}
              tone={emptyHarvest ? "warn" : undefined}
            />
            {(detail?.pages_failed ?? 0) > 0 && (
              <Metric label="실패" value={detail!.pages_failed} tone="bad" />
            )}
            <span className="text-xs text-muted-foreground">
              {duration(detail?.started_at ?? null, detail?.completed_at ?? null)}
            </span>

            {detail && <JobActions job={detail} busy={busy} setBusy={setBusy} refresh={job.refresh} />}
          </div>
        </div>
      </div>

      {detail?.error_message && (
        <div className="shrink-0 border-b bg-bad/10 px-6 py-2">
          <p className="mx-auto max-w-7xl text-sm text-bad">
            <span className="font-medium">{detail.error_kind}</span> — {detail.error_message}
          </p>
        </div>
      )}

      {emptyHarvest && !detail?.error_message && (
        <div className="shrink-0 border-b bg-warn/10 px-6 py-2">
          <p className="mx-auto max-w-7xl text-sm text-warn">
            페이지는 {detail.pages_crawled}개 가져왔지만 레코드가 0입니다 — 레시피 없이 돌렸거나,
            레시피의 셀렉터가 이 페이지들과 맞지 않습니다.
          </p>
        </div>
      )}

      <div className="mx-auto grid min-h-0 w-full max-w-7xl flex-1 gap-4 p-4 lg:grid-cols-[1fr_26rem]">
        <div className="flex min-h-0 flex-col">
          <div className="mb-3 flex shrink-0 gap-1 text-sm">
            <TabButton active={tab === "records"} onClick={() => setTab("records")}>
              레코드 {rows.length > 0 && <Count n={rows.length} />}
            </TabButton>
            <TabButton active={tab === "pages"} onClick={() => setTab("pages")}>
              페이지 {pageRows.length > 0 && <Count n={pageRows.length} />}
            </TabButton>
            <TabButton active={tab === "diagnostics"} onClick={() => setTab("diagnostics")}>
              진단
            </TabButton>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {tab === "records" && <RecordTable rows={rows} provenance={provenance} />}
            {tab === "pages" && <PageTable rows={pageRows} />}
            {tab === "diagnostics" && detail && <Diagnostics job={detail} />}
          </div>
        </div>

        <div className="min-h-0">
          <EventFeed events={stream.events} connected={stream.connected} ended={stream.ended} />
        </div>
      </div>
    </div>
  );
}

/**
 * Stop, run again, take the data away.
 *
 * Which of these is offered depends on the state, because pressing the wrong
 * one is refused by the API anyway and a button that always errors is worse
 * than no button. Export is always available - a cancelled crawl's records
 * are still records, which is the whole reason cancelling keeps them.
 */
function JobActions({
  job,
  busy,
  setBusy,
  refresh,
}: {
  job: JobDetail;
  busy: boolean;
  setBusy: (value: boolean) => void;
  refresh: () => void;
}) {
  const finished = TERMINAL_STATUSES.includes(job.status);
  const stopping = !finished && job.cancel_requested_at !== null;

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      {!finished && (
        <button
          type="button"
          disabled={busy || stopping}
          onClick={() => void act(() => api.cancel(job.id))}
          className="rounded-md border px-2.5 py-1 text-xs disabled:opacity-50"
        >
          {stopping ? "중지 중…" : "중지"}
        </button>
      )}

      {finished && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void act(() => api.retry(job.id))}
          className="rounded-md border px-2.5 py-1 text-xs disabled:opacity-50"
        >
          다시 실행
        </button>
      )}

      {job.records_extracted > 0 && (
        <>
          <a
            href={exportUrl(job.id, "csv", true)}
            className="rounded-md border px-2.5 py-1 text-xs hover:bg-muted"
          >
            CSV
          </a>
          <a
            href={exportUrl(job.id, "jsonl", true)}
            className="rounded-md border px-2.5 py-1 text-xs hover:bg-muted"
          >
            JSONL
          </a>
        </>
      )}
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone?: "warn" | "bad" }) {
  return (
    <span className={cn(tone === "warn" && "text-warn", tone === "bad" && "text-bad")}>
      {value}
      <span className="ml-1 text-xs text-muted-foreground">{label}</span>
    </span>
  );
}

function Count({ n }: { n: number }) {
  return <span className="ml-1 text-xs text-muted-foreground">{n}</span>;
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-md px-3 py-1.5 transition-colors",
        active ? "bg-muted font-medium" : "text-muted-foreground hover:bg-muted/50",
      )}
    >
      {children}
    </button>
  );
}

function PageTable({ rows }: { rows: PageRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        가져온 페이지가 없습니다.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <thead className="bg-muted/40 text-left">
          <tr>
            <th className="w-14 px-3 py-2 font-medium">상태</th>
            <th className="px-3 py-2 font-medium">URL</th>
            <th className="w-12 px-3 py-2 font-medium">깊이</th>
            <th className="w-20 px-3 py-2 text-right font-medium">크기</th>
            <th className="w-20 px-3 py-2 text-right font-medium">시간</th>
          </tr>
        </thead>
        <tbody className="divide-y font-mono text-xs">
          {rows.map((row, index) => (
            <tr key={index} className="hover:bg-muted/30">
              <td
                className={cn(
                  "px-3 py-1.5",
                  row.status_code === null
                    ? "text-bad"
                    : row.status_code >= 400
                      ? "text-warn"
                      : "text-ok",
                )}
              >
                {row.status_code ?? row.error_kind ?? "—"}
              </td>
              <td className="max-w-lg truncate px-3 py-1.5" title={row.url}>
                {shortUrl(row.url, 80)}
              </td>
              <td className="px-3 py-1.5 text-muted-foreground">{row.depth}</td>
              <td className="px-3 py-1.5 text-right text-muted-foreground">
                {bytes(row.content_length)}
              </td>
              <td className="px-3 py-1.5 text-right text-muted-foreground">
                {row.elapsed_ms ? `${row.elapsed_ms}ms` : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The two count maps the backend keeps.
 *
 * They exist because "400 pages failed" is not actionable and "380 of them
 * were blocked_429" is - it says to lower concurrency. The reject counts do
 * the same for URLs that never became fetches at all.
 */
function Diagnostics({ job }: { job: JobDetail }) {
  const sections: { title: string; hint: string; counts: Record<string, number> }[] = [
    {
      title: "실패한 페이지",
      hint: "blocked_429가 많으면 동시성을 낮추세요. blocked_403은 차단입니다.",
      counts: job.error_counts,
    },
    {
      title: "거부된 URL",
      hint: "scope가 대부분이면 정상입니다. pattern_budget이 많으면 크롤러 트랩을 만난 것입니다.",
      counts: job.reject_counts,
    },
  ];

  return (
    <div className="space-y-5">
      {sections.map((section) => {
        const entries = Object.entries(section.counts).sort((a, b) => b[1] - a[1]);
        return (
          <section key={section.title} className="space-y-2">
            <h3 className="text-sm font-medium">{section.title}</h3>
            {entries.length === 0 ? (
              <p className="text-sm text-muted-foreground">없음</p>
            ) : (
              <>
                <div className="overflow-hidden rounded-lg border">
                  {entries.map(([kind, count]) => (
                    <div
                      key={kind}
                      className="flex items-center justify-between border-b px-3 py-1.5 text-sm last:border-b-0"
                    >
                      <span className="font-mono text-xs">{kind}</span>
                      <span className="tabular-nums">{count}</span>
                    </div>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">{section.hint}</p>
              </>
            )}
          </section>
        );
      })}

      <section className="space-y-2">
        <h3 className="text-sm font-medium">실행</h3>
        <dl className="overflow-hidden rounded-lg border text-sm">
          {[
            ["워커", job.worker_id ?? "—"],
            ["큐 등록", new Date(job.created_at).toLocaleString()],
            ["시작", job.started_at ? new Date(job.started_at).toLocaleString() : "—"],
            ["종료", job.completed_at ? new Date(job.completed_at).toLocaleString() : "—"],
          ].map(([label, value]) => (
            <div key={label} className="flex justify-between border-b px-3 py-1.5 last:border-b-0">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="font-mono text-xs">{value}</dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}
