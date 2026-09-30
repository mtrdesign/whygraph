import type { PortChange, PortChangeAgent } from "../api";

// Dismissal of the port-change notices, remembered per port value: a later
// move to yet another port shows the notice again. Storage can be blocked or
// throw, so every access is guarded (then the notice simply shows again).

const KEY_PREFIX = "whygraph-port-change-dismissed:";

/** Storage scope: `portal` for the portal-level banner, `project:<slug>` per project. */
export function dismissedPort(scope: string): number | null {
  try {
    const raw = window.localStorage.getItem(KEY_PREFIX + scope);
    return raw !== null && /^\d+$/.test(raw) ? Number(raw) : null;
  } catch {
    return null;
  }
}

export function dismissPort(scope: string, port: number): void {
  try {
    window.localStorage.setItem(KEY_PREFIX + scope, String(port));
  } catch {
    // Private window / blocked storage: the notice comes back on reload.
  }
}

/** Whether an agent file still needs the developer (manual edit or an env hint). */
export function needsAttention(a: PortChangeAgent): boolean {
  return a.action === "manual" || a.action === "env" || a.action === "skipped";
}

/** Counts for the banner's one-line summary. */
export function summarizePortChange(change: PortChange) {
  const agents = change.projects.flatMap((p) => p.agents);
  return {
    rewritten:
      change.projects.filter((p) => p.markers === "rewritten").length +
      agents.filter((a) => a.action === "rewritten").length,
    manual: agents.filter((a) => a.action === "manual").length,
    unmounted: change.unmounted.length,
  };
}
