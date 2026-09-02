/**
 * The API's shapes, as the browser sees them.
 *
 * Hand-written rather than generated from the OpenAPI document. The surface is
 * a dozen types and a generator would be another build step to keep working;
 * if this drifts far enough to hurt, generation is the fix, not a bigger hand.
 */

export type JobStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export const TERMINAL_STATUSES: readonly JobStatus[] = ["completed", "failed", "cancelled"];

export interface JobSummary {
  id: string;
  status: JobStatus;
  pages_crawled: number;
  pages_failed: number;
  records_extracted: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface JobDetail extends JobSummary {
  worker_id: string | null;
  heartbeat_at: string | null;
  error_kind: string | null;
  error_message: string | null;
  /** Failures by kind. "400 failed" is not actionable; "380 blocked_429" is. */
  error_counts: Record<string, number>;
  /** URLs that never became fetches. A lot of `pattern_budget` means a trap. */
  reject_counts: Record<string, number>;
}

export interface RecordPage {
  job_id: string;
  total_returned: number;
  offset: number;
  limit: number;
  records: Record<string, unknown>[];
}

export interface PageRow {
  url: string;
  final_url: string | null;
  status_code: number | null;
  content_type: string | null;
  content_length: number | null;
  depth: number;
  elapsed_ms: number | null;
  error_kind: string | null;
  error_message: string | null;
  created_at: string;
}

export interface PageRowList {
  job_id: string;
  total_returned: number;
  offset: number;
  limit: number;
  pages: PageRow[];
}

export interface JobEvent {
  id: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface RecipeSummary {
  name: string;
  version: number;
  status: "candidate" | "preview" | "active" | "retired";
  source_url: string;
  allowed_domains: string[];
  container: string | null;
  field_names: string[];
  record_count: number;
  mean_fill: number;
  measured_at: string | null;
}

export interface RecipeDetail extends RecipeSummary {
  fields: { name: string; selector?: string; type?: string; attr?: string }[];
  fingerprint: string | null;
  notes: string | null;
}

/** What the submit form collects, before it becomes a CrawlSpec. */
export interface CrawlRequest {
  seed_urls: string[];
  allowed_domains: string[];
  mode: "collect" | "spider";
  follow_links: boolean;
  recipe?: string | null;
  limits: {
    max_pages: number;
    max_depth: number;
    global_concurrency?: number;
    per_host_concurrency?: number;
  };
}
