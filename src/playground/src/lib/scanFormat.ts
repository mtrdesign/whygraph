import { ApiError, type ScanRunRow, type ScanRunStatus, type ScanRunSummary } from "../api";
import { errorInfo } from "./apiErrors";
import { formatDateTime, formatUsd } from "./format";

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

type RunLike = Pick<ScanRunRow, "trigger" | "kind" | "analyze">;

/**
 * What a run was, for its title and the history: First scan, Import, Quick rescan,
 * Full rescan, Describe, Commit hook, GitHub push, Sync from GitHub or Scheduled
 * check (quick vs full is visible on every row).
 */
export function runLabel(run: RunLike): string {
  switch (run.trigger) {
    case "initial":
      return run.kind === "sync" ? "Import" : "First scan";
    case "manual":
      return run.analyze ? "Full rescan" : "Quick rescan";
    case "describe":
      return "Describe";
    case "hook":
      return "Commit hook";
    case "push":
      return "GitHub push";
    case "sync":
      return "Sync from GitHub";
    case "poll":
    case "reconcile":
      return "Scheduled check";
    default:
      return run.kind === "sync" ? "Sync from GitHub" : run.trigger;
  }
}

/** When a run entered the portal: queued, else started. */
const runMoment = (run: Pick<ScanRunRow, "queued_at" | "started_at">) => run.queued_at ?? run.started_at;

/**
 * A run's title, "<what> - <when>" (`Full rescan - 9 Oct 2026, 2:02 PM`); the run
 * id is only in the URL.
 */
export function runTitle(run: Pick<ScanRunRow, "trigger" | "kind" | "analyze" | "queued_at" | "started_at">): string {
  const when = runMoment(run);
  return when ? `${runLabel(run)} - ${formatDateTime(when)}` : runLabel(run);
}

/** `14:02` in the viewer's locale; `-` for a missing value. */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "-" : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** "System" (no requester), "You" (the viewer) or the requester's name. */
export function requesterLabel(run: Pick<ScanRunRow, "requested_by">, viewerUid?: string | null): string {
  const who = run.requested_by;
  if (!who) return "System";
  return viewerUid && who.uid === viewerUid ? "You" : who.label;
}

/** The "cost" half of the history: whether this payload carries cost at all (hidden = keys absent). */
export const carriesCost = (runs: Pick<ScanRunRow, "cost_usd" | "estimate_usd">[]): boolean =>
  runs.some((r) => "cost_usd" in r || "estimate_usd" in r);

/** "Cancelled by Ben" / "Cancelled by you" (the viewer) / the budget stop; `null` when nobody cancelled it. */
export function cancelledLabel(
  run: Pick<ScanRunRow, "cancelled_by" | "summary">,
  viewerUid?: string | null,
): string {
  if (run.summary?.cancelled_by === "budget") return BUDGET_STOPPED;
  const who = run.cancelled_by;
  if (!who) return "Cancelled";
  return viewerUid && who.uid === viewerUid ? "Cancelled by you" : `Cancelled by ${who.label}`;
}

/** The registry code, the follow-up page and a short title for a failure the run's text names. */
interface FailurePattern {
  test: RegExp;
  code: string;
  settings?: "models" | "github";
}

const FAILURES: FailurePattern[] = [
  { test: /github.*(refused|cannot reach|deleted)|repository was deleted|reconnect it on github/i, code: "github_access_lost" },
  { test: /symbolic link|refusing to scan/i, code: "unsafe_path" },
  { test: /budget/i, code: "budget_exceeded" },
  { test: /api[ _-]?key|no .* key|invalid_api_key|incorrect api key/i, code: "no_llm_key", settings: "models" },
  { test: /could not get a github token|could not resolve host|timed out|unreachable|connection (refused|reset)|network/i, code: "github_unavailable" },
  { test: /github token|bad credentials|token .*(invalid|expired|revoked)/i, code: "bad_token", settings: "github" },
];

export interface FailureSummary {
  /** The registry's sentence for a known cause, else a generic one. */
  message: string;
  /** Where the fix is, when it is a setting. */
  settings: "models" | "github" | null;
  /** Whether the cause is one the runner's text names (the sentence is the registry's). */
  known: boolean;
  /** The run's own words (and the failed crawlers'), for "Show details". */
  details: string[];
}

/**
 * A human summary of a failed run: the registry's sentence for the causes the
 * runner's text names (access lost, a tracked state, a missing key or token,
 * GitHub unreachable, a budget), else a generic one with the raw text under
 * details.
 */
export function failureSummary(
  summary: Pick<ScanRunSummary, "error" | "crawlers" | "exit_code"> | null | undefined,
  streamError?: string | null,
): FailureSummary {
  const failed = (summary?.crawlers ?? []).filter((c) => c.status === "failed");
  const text = streamError ?? (typeof summary?.error === "string" ? summary.error : "");
  const details = [
    ...(text ? [text] : []),
    ...failed.map((c) => `${c.name}: ${c.error ?? "failed"}`),
    ...(!text && !failed.length && typeof summary?.exit_code === "number" ? [`Exit code ${summary.exit_code}`] : []),
  ];
  const hay = [text, ...failed.map((c) => c.error ?? "")].join("\n");
  const known = FAILURES.find((f) => f.test.test(hay));
  if (known) {
    const info = errorInfo(new ApiError(409, text, known.code));
    return { message: info.message, settings: known.settings ?? null, details, known: true };
  }
  return {
    message: failed.length
      ? "Part of the scan failed. Nothing was lost; run it again, or open the log for the detail."
      : "The scan stopped before it finished. Nothing was lost; run it again, or open the log for the detail.",
    settings: null,
    details,
    known: false,
  };
}

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

/**
 * What a finished sync did, in the words of the run page and the history:
 * "Imported and scanned", "Fetched new commits and scanned", "No new commits"
 * (never on a first run) or `null` when none of them applies.
 */
export function syncOutcome(
  run: Pick<ScanRunRow, "trigger">,
  summary: Pick<ScanRunSummary, "cloned" | "scanned" | "moved"> | null | undefined,
): string | null {
  if (summary?.cloned) return "Imported and scanned";
  if (summary?.moved && summary.scanned) return "Fetched new commits and scanned";
  if (summary?.moved) return "Fetched new commits";
  if (summary?.moved === false && run.trigger !== "initial") return "No new commits";
  return null;
}

/** A one-line result for the history list (what the run did, or why it stopped). */
export function runOutcome(run: ScanRunRow, viewerUid?: string | null): string | null {
  const s = run.summary;
  if (run.status === "cancelled") return cancelledLabel(run, viewerUid);
  if (!s) return null;
  if (typeof s.merged_into === "number") return "Covered by another run";
  if (run.status === "failed" || run.status === "interrupted") {
    const failure = failureSummary(s);
    if (failure.known) return failure.message;
    if (typeof s.error === "string") return s.error;
    const failed = s.crawlers?.find((c) => c.status === "failed");
    if (failed) return `${failed.name}: ${failed.error ?? "failed"}`;
    return typeof s.exit_code === "number" ? `Exit code ${s.exit_code}` : null;
  }
  if (run.kind === "sync") {
    const synced = syncOutcome(run, s);
    if (synced) return synced;
  }
  // `--skip-analyze` is the requested structure-only scan; anything else is the
  // reason descriptions could not run (no key, unreachable endpoint).
  if (s.analyze_skipped === "budget") return BUDGET_SKIPPED;
  if (s.analyze_skipped === "--codegraph-only") return CODE_INDEX_REFRESHED;
  if (s.analyze_skipped) return s.analyze_skipped === "--skip-analyze" ? "Structure only" : "Descriptions skipped";
  if (run.status !== "ok") return null;
  return run.analyze ? "With descriptions" : "Structure only";
}
