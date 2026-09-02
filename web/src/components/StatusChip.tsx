import { cn } from "@/lib/format";
import type { JobStatus } from "@/lib/types";

const STYLES: Record<JobStatus, string> = {
  queued: "bg-muted text-muted-foreground",
  running: "bg-running/15 text-running",
  completed: "bg-ok/15 text-ok",
  failed: "bg-bad/15 text-bad",
  cancelled: "bg-muted text-muted-foreground",
};

const LABELS: Record<JobStatus, string> = {
  queued: "대기",
  running: "실행 중",
  completed: "완료",
  failed: "실패",
  cancelled: "취소",
};

export function StatusChip({ status }: { status: JobStatus }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium",
        STYLES[status] ?? STYLES.queued,
      )}
    >
      {status === "running" && (
        <span className="size-1.5 animate-pulse rounded-full bg-running" aria-hidden />
      )}
      {LABELS[status] ?? status}
    </span>
  );
}

export function RecipeStatusChip({ status }: { status: string }) {
  const style =
    status === "active"
      ? "bg-ok/15 text-ok"
      : status === "retired"
        ? "bg-muted text-muted-foreground"
        : "bg-warn/15 text-warn";
  return (
    <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", style)}>{status}</span>
  );
}
