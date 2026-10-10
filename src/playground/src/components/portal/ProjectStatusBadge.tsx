import type { ProjectSummary } from "../../api";
import { projectStatus, type StatusTone } from "../../lib/projectStatus";
import { StatusPill, type StatusPillTone } from "../ui/status-pill";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

const TONE: Record<StatusTone, StatusPillTone> = {
  ready: "ok",
  busy: "busy",
  warn: "warn",
  error: "error",
  idle: "idle",
};

/**
 * The project's status as a soft pill (`Ready`, `Behind`, `Stopped` ...). A
 * status with more to say (`Stopped`: "Monthly budget reached") carries it as a
 * tooltip, and for screen readers after the label.
 */
export function ProjectStatusBadge({ project }: { project: ProjectSummary }) {
  const s = projectStatus(project);
  const pill = (
    <StatusPill tone={TONE[s.tone]} label={s.label} data-status={s.key} pulse={s.key === "scanning" || s.key === "importing"} />
  );
  if (!s.tooltip) return pill;
  return (
    <Tooltip>
      <TooltipTrigger render={<span className="relative z-10 inline-flex shrink-0" tabIndex={0} />}>
        {pill}
        <span className="sr-only">: {s.tooltip}</span>
      </TooltipTrigger>
      <TooltipContent>{s.tooltip}</TooltipContent>
    </Tooltip>
  );
}
