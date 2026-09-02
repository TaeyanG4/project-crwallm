"use client";

/**
 * Tail one job's event log over SSE.
 *
 * The counters on a job row say a crawl is moving. They do not say what it is
 * doing, and when a crawl finds nothing that is the only question worth
 * answering - the events distinguish "every URL was rejected as out of scope"
 * from "every page 404ed" from "the pages were fine and the recipe matched
 * nothing".
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { streamUrl } from "@/lib/api";
import type { JobEvent } from "@/lib/types";

/** Keep the feed bounded: a big spider run emits far more than a browser
 *  should hold, and only the recent tail is ever read. */
const MAX_KEPT = 600;

export interface StreamState {
  events: JobEvent[];
  /** True while the connection is open - not the same as the job running. */
  connected: boolean;
  /** Set once the server says the log is drained. */
  ended: boolean;
  error: string | null;
}

export function useJobStream(jobId: string, enabled = true): StreamState {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [ended, setEnded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The cursor lives in a ref, not state: a reconnect must resume from the
  // last id actually seen, and re-running the effect on every event would
  // reconnect the stream on every event.
  const cursor = useRef(0);

  const push = useCallback((event: JobEvent) => {
    cursor.current = Math.max(cursor.current, event.id);
    setEvents((previous) => {
      const next = [...previous, event];
      return next.length > MAX_KEPT ? next.slice(next.length - MAX_KEPT) : next;
    });
  }, []);

  useEffect(() => {
    if (!enabled || !jobId) return;

    let source: EventSource | null = null;
    let closed = false;

    const open = () => {
      if (closed) return;
      source = new EventSource(streamUrl(jobId, cursor.current));

      source.onopen = () => {
        setConnected(true);
        setError(null);
      };

      // Every crawl event type arrives as its own SSE event name, so one
      // generic listener has to be attached rather than `onmessage`.
      source.onmessage = (message) => {
        try {
          push(JSON.parse(message.data) as JobEvent);
        } catch {
          // A frame we cannot parse is not worth killing the feed over.
        }
      };

      for (const type of EVENT_TYPES) {
        source.addEventListener(type, (message) => {
          try {
            push(JSON.parse((message as MessageEvent).data) as JobEvent);
          } catch {
            /* ignore */
          }
        });
      }

      source.addEventListener("end", () => {
        closed = true;
        setEnded(true);
        setConnected(false);
        source?.close();
      });

      source.addEventListener("timeout", () => {
        closed = true;
        setConnected(false);
        source?.close();
      });

      source.onerror = () => {
        setConnected(false);
        // EventSource reconnects by itself and sends Last-Event-ID, which the
        // backend honours. Nothing to do but say the feed is not live.
        if (closed) source?.close();
      };
    };

    open();

    return () => {
      closed = true;
      source?.close();
      setConnected(false);
    };
  }, [jobId, enabled, push]);

  return { events, connected, ended, error };
}

/**
 * The event names the backend emits (`CrawlEvent.type`).
 *
 * Listed explicitly because SSE dispatches by name: a frame sent as
 * `event: page.fetched` never reaches `onmessage`.
 */
export const EVENT_TYPES = [
  "job.started",
  "job.completed",
  "job.failed",
  "job.cancelled",
  "page.fetched",
  "page.failed",
  "links.discovered",
  "url.rejected",
  "pattern.budget_exhausted",
  "duplicate.detected",
  "records.extracted",
  "records.filtered",
  "progress",
] as const;
