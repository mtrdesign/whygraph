// What the budget banner says and remembers (plan section 4.8). The banners are
// computed from `state.usage`; the only state kept is a per-viewer dismissal of the
// 50 / 75 notices for the month, in `localStorage` (a convenience: it can be absent).

/** The thresholds a budget alerts at. */
export const THRESHOLDS = [50, 75, 100] as const;
export type Threshold = (typeof THRESHOLDS)[number];

/** The highest threshold `pct` has reached, or `null` below 50% (or without a budget). */
export function thresholdOf(pct: number | null | undefined): Threshold | null {
  if (pct === null || pct === undefined) return null;
  for (let i = THRESHOLDS.length - 1; i >= 0; i--) if (pct >= THRESHOLDS[i]) return THRESHOLDS[i];
  return null;
}

/** The sentence a member at 100% with a hard stop sees. */
export const MEMBER_STOPPED_LINE =
  "You've reached your monthly budget. You can still read everything that's already generated.";

/**
 * The notice for a hard-stopped budget at `scope` (who ran out of budget). The same words
 * show on the project pages, in Chat and on Generate; `member` is the roadmap's line.
 */
export function budgetNoticeText(scope: string | null | undefined): string {
  if (scope === "member") return MEMBER_STOPPED_LINE;
  const whose = scope === "project" ? "This project's" : scope === "org" ? "This organization's" : "The";
  return `${whose} monthly LLM budget is reached, so chat and generating are paused. You can still read everything that's already generated.`;
}

const key = (scope: string, month: string, threshold: Threshold) =>
  `whygraph:budget-banner:${scope}:${month}:${threshold}`;

/** Whether the viewer dismissed this scope's `threshold` notice this `month`. */
export function isDismissed(scope: string, month: string, threshold: Threshold): boolean {
  try {
    return window.localStorage.getItem(key(scope, month, threshold)) === "1";
  } catch {
    return false;
  }
}

/** Remember a dismissal (a no-op where storage is blocked). */
export function dismiss(scope: string, month: string, threshold: Threshold): void {
  try {
    window.localStorage.setItem(key(scope, month, threshold), "1");
  } catch {
    // Storage blocked: the banner simply comes back on the next load.
  }
}
