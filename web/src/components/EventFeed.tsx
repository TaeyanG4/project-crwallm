"use client";

/**
 * The crawl, as it happens.
 *
 * Deliberately not a raw log dump. Each event type gets the one line that
 * makes it legible - a rejection says *why* it was rejected, a fetch says its
 * status and how long it took - because the feed is read while it scrolls.
 */

import { useEffect, useRef, useState } from "react";
import { cn, shortUrl } from "@/lib/format";
import type { JobEvent } from "@/lib/types";

interface Rendered {
  tone: "ok" | "bad" | "warn" | "muted" | "info";
  label: string;
  text: string;
  detail?: string;
}

function render(event: JobEvent): Rendered {
  const p = event.payload as Record<string, never>;
  const url = (p.url as string | undefined) ?? "";

  switch (event.event_type) {
    case "job.started":
      return {
        tone: "info",
        label: "시작",
        text: `${((p.seeds as string[] | undefined) ?? []).length}개 시드`,
      };
    case "page.fetched":
      return {
        tone: (p.status as number) >= 400 ? "warn" : "ok",
        label: String(p.status ?? ""),
        text: shortUrl(url),
        detail: `${p.elapsed_ms ?? "?"}ms · d${p.depth ?? 0}`,
      };
    case "page.failed":
      return {
        tone: "bad",
        label: "실패",
        text: shortUrl(url),
        detail: String(p.error_kind ?? ""),
      };
    case "records.extracted":
      return {
        tone: "ok",
        label: `+${p.count ?? 0}`,
        text: shortUrl(url),
        detail: "레코드",
      };
    case "records.filtered":
      return {
        tone: "muted",
        label: `−${p.removed ?? 0}`,
        text: shortUrl(url),
        detail: "필터",
      };
    case "links.discovered":
      return {
        tone: "muted",
        label: "링크",
        text: shortUrl(url),
        // Found vs enqueued is the useful pair: a big gap means the scope or
        // the filters are doing most of the work.
        detail: `${p.found ?? 0} 발견 → ${p.enqueued ?? 0} 큐`,
      };
    case "url.rejected":
      return {
        tone: "muted",
        label: "거부",
        text: shortUrl(url),
        detail: String(p.reason ?? ""),
      };
    case "duplicate.detected":
      return {
        tone: "muted",
        label: "중복",
        text: shortUrl(url),
        detail: String(p.via ?? ""),
      };
    case "pattern.budget_exhausted":
      return {
        tone: "warn",
        label: "예산 소진",
        text: String(p.pattern ?? ""),
        detail: "이 URL 모양은 더 가져오지 않음",
      };
    case "progress":
      return {
        tone: "muted",
        label: "진행",
        text: `${p.fetched ?? 0} 완료 · ${p.queued ?? 0} 대기`,
        detail: p.hosts_active ? `호스트 ${p.hosts_active}` : undefined,
      };
    case "job.completed":
      return {
        tone: "ok",
        label: "완료",
        text: `${p.pages_fetched ?? 0} 페이지 · ${p.records_extracted ?? 0} 레코드`,
        detail: `${p.elapsed_s ?? "?"}s`,
      };
    case "job.failed":
      return { tone: "bad", label: "실패", text: String(p.message ?? "") };
    case "job.cancelled":
      return { tone: "warn", label: "취소", text: "" };
    default:
      return { tone: "muted", label: event.event_type, text: "" };
  }
}

const TONE: Record<Rendered["tone"], string> = {
  ok: "text-ok",
  bad: "text-bad",
  warn: "text-warn",
  info: "text-running",
  muted: "text-muted-foreground",
};

export function EventFeed({
  events,
  connected,
  ended,
}: {
  events: JobEvent[];
  connected: boolean;
  ended: boolean;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);

  // Follow the tail, but stop the moment the reader scrolls up - yanking them
  // back to the bottom while they are reading an error is the worst thing a
  // live feed can do.
  useEffect(() => {
    if (!pinned) return;
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events, pinned]);

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-lg border">
      <div className="flex items-center justify-between border-b bg-muted/40 px-3 py-2">
        <span className="text-xs font-medium">이벤트</span>
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {connected ? (
            <>
              <span className="size-1.5 animate-pulse rounded-full bg-running" />
              라이브
            </>
          ) : ended ? (
            "종료"
          ) : (
            "연결 안 됨"
          )}
          <span className="ml-2 tabular-nums">{events.length}</span>
        </span>
      </div>

      <div
        ref={scroller}
        className="feed flex-1 overflow-y-auto p-1 font-mono text-xs"
        onScroll={(e) => {
          const el = e.currentTarget;
          setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
        }}
      >
        {events.length === 0 && (
          <p className="p-3 text-muted-foreground">아직 이벤트가 없습니다.</p>
        )}
        {events.map((event) => {
          const r = render(event);
          return (
            <div
              key={event.id}
              className="flex items-baseline gap-2 rounded px-2 py-0.5 hover:bg-muted/50"
            >
              <span className={cn("w-16 shrink-0 text-right", TONE[r.tone])}>{r.label}</span>
              <span className="min-w-0 flex-1 truncate">{r.text}</span>
              {r.detail && (
                <span className="shrink-0 text-muted-foreground">{r.detail}</span>
              )}
            </div>
          );
        })}
      </div>

      {!pinned && (
        <button
          type="button"
          onClick={() => setPinned(true)}
          className="border-t bg-muted/40 py-1.5 text-xs text-muted-foreground hover:bg-muted"
        >
          ↓ 최신으로
        </button>
      )}
    </div>
  );
}
