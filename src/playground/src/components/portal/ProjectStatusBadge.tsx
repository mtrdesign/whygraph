import { cn } from "@/lib/utils";
import type { ProjectSummary } from "../../api";
import { projectStatus, type StatusTone } from "../../lib/projectStatus";

const TONE: Record<StatusTone, string> = {
  ready: "bg-success",
  busy: "bg-primary animate-pulse",
  warn: "bg-warning",
  error: "bg-destructive",
  idle: "bg-muted-foreground/50",
};

/** The project's status as a dot and a label (`Ready`, `Stale, 3 commits behind` ...). */
export function ProjectStatusBadge({ project }: { project: ProjectSummary }) {
  const s = projectStatus(project);
  return (
    <span className="flex items-center gap-1.5 text-xs" data-status={s.key}>
      <span className={cn("size-2 rounded-full", TONE[s.tone])} aria-hidden />
      {s.label}
    </span>
  );
}
