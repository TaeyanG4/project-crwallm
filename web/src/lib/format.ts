/** Small presentation helpers. Nothing here makes a decision. */

export function cn(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 5) return "방금";
  if (seconds < 60) return `${seconds}초 전`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  return new Date(iso).toLocaleDateString();
}

export function duration(startIso: string | null, endIso: string | null): string {
  if (!startIso) return "—";
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  const seconds = (end - start) / 1000;
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * Shorten a URL for a table cell without losing which page it is.
 *
 * The host repeats on every row of a single-site crawl and the path is what
 * distinguishes them, so the path is what survives.
 */
export function shortUrl(raw: string, max = 60): string {
  let path: string;
  try {
    const url = new URL(raw);
    path = url.pathname + url.search;
  } catch {
    path = raw;
  }
  if (path.length <= max) return path || "/";
  return `${path.slice(0, max - 12)}…${path.slice(-10)}`;
}

/**
 * Column names for a set of records, in the order they were first seen.
 *
 * A recipe's fields are the usual answer, but records also arrive from the
 * chat and from ad-hoc extraction, so the shape is read from the rows rather
 * than assumed.
 */
export function columnsOf(rows: Record<string, unknown>[]): string[] {
  const seen: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!seen.includes(key)) seen.push(key);
    }
  }
  return seen;
}

export function cellText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}
