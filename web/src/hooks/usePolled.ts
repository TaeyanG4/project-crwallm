"use client";

/**
 * Fetch something, and keep fetching it while it can still change.
 *
 * Both list and detail screens need the same three things: load once, poll
 * while a crawl is moving, stop when it is not. Writing that at each call site
 * produced three copies that each forgot something different - this one drops
 * results that arrive after the component is gone, which is what turns a fast
 * click-through into a console full of state-update warnings.
 *
 * `intervalMs` of `null` means "load once". That is the same code path rather
 * than a second one, so a screen switches between polling and not by changing
 * a number.
 */

import { useEffect, useState } from "react";

export interface Polled<T> {
  data: T | null;
  error: string | null;
  loaded: boolean;
  /** Force a refetch - after submitting something, say. */
  refresh: () => void;
}

export function usePolled<T>(
  fetcher: () => Promise<T>,
  intervalMs: number | null,
  deps: readonly unknown[] = [],
): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;

    const tick = async () => {
      try {
        const next = await fetcher();
        if (!alive) return;
        setData(next);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setLoaded(true);
      }
    };

    void tick();

    if (intervalMs === null) {
      return () => {
        alive = false;
      };
    }

    const timer = setInterval(() => void tick(), intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // `fetcher` is intentionally not a dependency: it is an inline closure at
    // every call site, so depending on it would restart the poll on every
    // render. The caller declares what the fetch actually depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, nonce, ...deps]);

  return { data, error, loaded, refresh: () => setNonce((n) => n + 1) };
}
