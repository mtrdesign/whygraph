import type { ScanRunRow, ScanRunStatus, ScanRunSummary } from "../api";
import { formatUsd } from "./format";

/** What a hard stop's cancelled run says (history list and run page). */
export const BUDGET_STOPPED = "Stopped: monthly budget reached";
/** What a run that skipped its LLM phase on a hard stop says. */
export const BUDGET_SKIPPED = "LLM phase skipped: monthly budget reached";
/** A linked project's run (`--codegraph-only`): only the code index, its history is on the platform. */
export const CODE_INDEX_REFRESHED = "Code index refreshed";

/** "LLM usage: ~$0.42, 12 calls", only when the run made calls. */
export function usageLine(summary: Pick<ScanRunSummary, "usage"> | null | undefined): string | null {
  const u = summary?.usage;
  if (!u || !(u.calls > 0)) return null;
  return `LLM usage: ~${formatUsd(u.cost_usd)}, ${u.calls} ${u.calls === 1 ? "call" : "calls"}`;
}

/** `4.1` -> `4.1s`, `128` -> `2m 08s`, `3700` -> `1h 01m`. */
export function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const total = Math.round(seconds);
  if (total < 3600) return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
  return `${Math.floor(total / 3600)}h ${String(Math.floor((total % 3600) / 60)).padStart(2, "0")}m`;
}

/**
 * How long a run took: the child's own `elapsed_sec` when it reported one, else
 * the wall time between `started_at` and `finished_at` (a failed or interrupted
 * run has no `result`), else `null` while it is still going or never started.
 */
export function runSeconds(run: Pick<ScanRunRow, "started_at" | "finished_at" | "summary">): number | null {
  const reported = run.summary?.elapsed_sec;
  if (typeof reported === "number") return reported;
  if (!run.started_at || !run.finished_at) return null;
  const ms = Date.parse(run.finished_at) - Date.parse(run.started_at);
  return Number.isNaN(ms) || ms < 0 ? null : ms / 1000;
}

export { triggerLabel } from "./labels";

export type RunTone = "ok" | "busy" | "warn" | "error" | "idle";

const STATUS: Record<ScanRunStatus, { label: string; tone: RunTone }> = {
  queued: { label: "Queued", tone: "idle" },
  running: { label: "Running", tone: "busy" },
  ok: { label: "Succeeded", tone: "ok" },
  failed: { label: "Failed", tone: "error" },
  interrupted: { label: "Interrupted", tone: "warn" },
  cancelled: { label: "Cancelled", tone: "idle" },
};

export const runStatus = (status: ScanRunStatus) =>
  STATUS[status] ?? { label: status, tone: "idle" as RunTone };

/** A one-line result for the history list (what the run did, or why it stopped). */
export function runOutcome(run: ScanRunRow): string | null {
  const s = run.summary;
  if (!s) return null;
  if (typeof s.merged_into === "number") return `Merged into run #${s.merged_into}`;
  if (s.cancelled_by === "user") return "Cancelled by you";
  if (s.cancelled_by === "budget") return BUDGET_STOPPED;
  if (run.status === "failed" || run.status === "interrupted") {
    if (typeof s.error === "string") return s.error;
    const failed = s.crawlers?.find((c) => c.status === "failed");
    if (failed) return `${failed.name}: ${failed.error ?? "failed"}`;
    return typeof s.exit_code === "number" ? `Exit code ${s.exit_code}` : null;
  }
  if (run.kind === "sync") return s.moved ? "Fetched new commits" : "Already up to date";
  // `--skip-analyze` is the requested structure-only scan; anything else is the
  // reason descriptions could not run (no key, unreachable endpoint).
  if (s.analyze_skipped === "budget") return BUDGET_SKIPPED;
  if (s.analyze_skipped === "--codegraph-only") return CODE_INDEX_REFRESHED;
  if (s.analyze_skipped) return s.analyze_skipped === "--skip-analyze" ? "Structure only" : "Descriptions skipped";
  return run.analyze ? "With descriptions" : "Structure only";
}
