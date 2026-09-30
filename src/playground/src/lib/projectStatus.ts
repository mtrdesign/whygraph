import type { ProjectSummary } from "../api";

export type StatusTone = "ready" | "busy" | "warn" | "error" | "idle";

export interface ProjectStatus {
  key: "unavailable" | "uninitialized" | "scanning" | "failed" | "stale" | "unscanned" | "ready";
  label: string;
  tone: StatusTone;
}

/**
 * The status badge of a project card (§4.9.1 screen 2), by precedence: an
 * unusable folder beats everything, then "not initialized", a running scan,
 * a failed / interrupted last scan, staleness, never-scanned, and finally ready.
 */
export function projectStatus(p: ProjectSummary): ProjectStatus {
  if (p.root_status !== "ok") {
    return {
      key: "unavailable",
      label: p.root_status === "missing" ? "Folder missing" : "Not a git repository",
      tone: "error",
    };
  }
  if (!p.initialized) return { key: "uninitialized", label: "Not initialized", tone: "idle" };
  if (p.running_scan) {
    return {
      key: "scanning",
      label: p.running_scan.status === "queued" ? "Scan queued" : "Scanning",
      tone: "busy",
    };
  }
  // The newest ended run did not finish: say so before anything softer.
  if (p.last_scan_status === "failed") return { key: "failed", label: "Scan failed", tone: "error" };
  if (p.last_scan_status === "interrupted") {
    return { key: "failed", label: "Scan interrupted", tone: "warn" };
  }
  if (p.stale) {
    const n = p.stale.commits_behind;
    return {
      key: "stale",
      label: n === null ? "Stale" : `Stale, ${n} commit${n === 1 ? "" : "s"} behind`,
      tone: "warn",
    };
  }
  if (!p.last_scan_at) return { key: "unscanned", label: "Not scanned yet", tone: "idle" };
  return { key: "ready", label: "Ready", tone: "ready" };
}

/** `2026-09-30T12:00:00+00:00` -> `12 min ago`. Falls back to the date beyond a month. */
export function timeAgo(iso: string | null, now: number = Date.now()): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const sec = Math.max(0, Math.round((now - then) / 1000));
  if (sec < 60) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hours = Math.round(min / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 31) return `${days} d ago`;
  return new Date(then).toLocaleDateString();
}
