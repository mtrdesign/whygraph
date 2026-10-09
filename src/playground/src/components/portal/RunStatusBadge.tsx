import type { ScanRunStatus } from "../../api";
import { runStatus, type RunTone } from "../../lib/scanFormat";
import { StatusPill, type StatusPillTone } from "../ui/status-pill";

const TONE: Record<RunTone, StatusPillTone> = {
  ok: "ok",
  busy: "busy",
  warn: "warn",
  error: "error",
  idle: "idle",
};

/** A run's status as a soft pill (`Running`, `Succeeded`, `Interrupted` ...). */
export function RunStatusBadge({ status, className }: { status: ScanRunStatus; className?: string }) {
  const s = runStatus(status);
  return <StatusPill tone={TONE[s.tone]} label={s.label} className={className} data-status={status} />;
}
