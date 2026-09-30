import { cn } from "@/lib/utils";
import type { ScanRunStatus } from "../../api";
import { runStatus, type RunTone } from "../../lib/scanFormat";

const DOT: Record<RunTone, string> = {
  ok: "bg-success",
  busy: "bg-primary animate-pulse",
  warn: "bg-warning",
  error: "bg-destructive",
  idle: "bg-muted-foreground/50",
};

/** A run's status as a dot and a word (`Running`, `Succeeded`, `Interrupted` ...). */
export function RunStatusBadge({ status, className }: { status: ScanRunStatus; className?: string }) {
  const s = runStatus(status);
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs", className)} data-status={status}>
      <span className={cn("size-2 rounded-full", DOT[s.tone])} aria-hidden />
      {s.label}
    </span>
  );
}
