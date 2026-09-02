/**
 * Browser-side client for the crawler API.
 *
 * Every call goes to `/api/crwallm/...` on this origin, which the proxy route
 * forwards with the token attached. Nothing here knows the token exists.
 */

import type {
  CrawlRequest,
  JobDetail,
  JobEvent,
  JobSummary,
  PageRowList,
  RecipeDetail,
  RecipeSummary,
  RecordPage,
} from "./types";

const BASE = "/api/crwallm";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { accept: "application/json", ...init?.headers },
  });

  if (!response.ok) {
    // FastAPI puts the reason in `detail`, either a string or a list of
    // validation errors. Both are more useful than the status code alone.
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (Array.isArray(body.detail)) {
        detail = body.detail
          .map((e: { loc?: string[]; msg?: string }) =>
            [e.loc?.slice(1).join("."), e.msg].filter(Boolean).join(": "),
          )
          .join("; ");
      }
    } catch {
      // Not JSON. The status line stands.
    }
    throw new ApiError(response.status, detail);
  }

  return response.json() as Promise<T>;
}

export const api = {
  listJobs: (limit = 30) => request<JobSummary[]>(`/jobs?limit=${limit}`),

  getJob: (id: string) => request<JobDetail>(`/jobs/${id}`),

  getRecords: (id: string, limit = 100, offset = 0) =>
    request<RecordPage>(`/jobs/${id}/results?limit=${limit}&offset=${offset}`),

  getPages: (id: string, limit = 200, offset = 0) =>
    request<PageRowList>(`/jobs/${id}/pages?limit=${limit}&offset=${offset}`),

  getEvents: (id: string, after = 0, limit = 500) =>
    request<JobEvent[]>(`/jobs/${id}/events?after=${after}&limit=${limit}`),

  listRecipes: () => request<RecipeSummary[]>("/recipes"),

  getRecipe: (name: string) => request<RecipeDetail>(`/recipes/${encodeURIComponent(name)}`),

  cancel: (id: string) => request<JobDetail>(`/jobs/${id}/cancel`, { method: "POST" }),

  retry: (id: string) => request<JobDetail>(`/jobs/${id}/retry`, { method: "POST" }),

  submit: (spec: CrawlRequest, priority = 0) =>
    request<{ id: string; status: string; created_at: string }>("/jobs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ spec, priority }),
    }),
};

/** A download URL, not a fetch: the file streams and the browser saves it. */
export function exportUrl(jobId: string, format: "jsonl" | "csv", withSource = false): string {
  return `${BASE}/jobs/${jobId}/export?format=${format}&include_source=${withSource}`;
}

/** The SSE endpoint for one job, through the proxy. */
export function streamUrl(jobId: string, after = 0): string {
  return `${BASE}/jobs/${jobId}/stream?after=${after}`;
}
