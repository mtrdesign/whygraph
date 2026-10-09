import type { ProjectSummary } from "../../api";
import { projectStatus, type StatusTone } from "../../lib/projectStatus";
import { StatusPill, type StatusPillTone } from "../ui/status-pill";

const TONE: Record<StatusTone, StatusPillTone> = {
  ready: "ok",
  busy: "busy",
  warn: "warn",
  error: "error",
  idle: "idle",
};

/** The project's status as a soft pill (`Ready`, `Stale, 3 commits behind` ...). */
export function ProjectStatusBadge({ project }: { project: ProjectSummary }) {
  const s = projectStatus(project);
  return <StatusPill tone={TONE[s.tone]} label={s.label} data-status={s.key} />;
}
