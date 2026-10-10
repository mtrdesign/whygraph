import type { OverviewEventKind, ProjectOverview } from "../api";
import type { ChartMarker, ChartPayload } from "../components/charts/chartSpec";

// The project Overview's derived data: the coverage-over-time chart with its
// event markers, the agent-activity bars, and the words for agent call kinds.

/** What each agent call kind is, in words (MCP registered names and the `/api/v1` routes). */
const AGENT_KINDS: Record<string, string> = {
  whygraph_evidence_for: "Evidence lookups",
  whygraph_rationale_brief: "Rationale cards",
  whygraph_area_history: "Area history",
  whygraph_repo_overview: "Repository overview",
  whygraph_commit: "Commit lookups",
  whygraph_pull_request: "Pull request lookups",
  whygraph_issue: "Issue lookups",
  whygraph_pre_edit_brief: "Pre-edit briefs",
  whygraph_triage_commit: "Commit triage",
  whygraph_why_was_this_written: "Why was this written",
  "v1:evidence": "Evidence lookups",
  "v1:rationale": "Rationale cards",
  "v1:history": "Area history",
  "v1:commit": "Commit lookups",
  "v1:pr": "Pull request lookups",
  "v1:issue": "Issue lookups",
  "v1:overview": "Repository overview",
};

/** `Evidence lookups` for `whygraph_evidence_for`; an unknown kind is shown as sent. */
export function agentKindLabel(kind: string): string {
  return AGENT_KINDS[kind] ?? kind;
}

export const EVENT_LABEL: Record<OverviewEventKind, string> = {
  import: "Import",
  full_scan: "Full scan",
  describe: "Descriptions",
  failed: "Failed run",
  budget_stop: "Budget stop",
};

export const EVENT_TONE: Record<OverviewEventKind, ChartMarker["tone"]> = {
  import: "info",
  full_scan: "ok",
  describe: "ok",
  failed: "error",
  budget_stop: "warn",
};

const DAY_MONTH = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
// `days` are UTC dates; printing them in the viewer's zone could shift a day.
const DAY_UTC = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", timeZone: "UTC" });
const DAY_MONTH_TIME = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  hour: "numeric",
  minute: "2-digit",
});

/** `9 Oct`, with the time when two scans share a day, so every category is unique. */
function pointLabels(ats: string[]): string[] {
  const days = ats.map((at) => DAY_MONTH.format(new Date(at)));
  const count = new Map<string, number>();
  for (const d of days) count.set(d, (count.get(d) ?? 0) + 1);
  const labels = ats.map((at, i) => ((count.get(days[i]) ?? 0) > 1 ? DAY_MONTH_TIME.format(new Date(at)) : days[i]));
  // Two scans in the same minute: number the repeats.
  const seen = new Map<string, number>();
  return labels.map((l) => {
    const n = (seen.get(l) ?? 0) + 1;
    seen.set(l, n);
    return n > 1 ? `${l} (${n})` : l;
  });
}

/**
 * The "Commits described" line over the scans that recorded coverage, with a
 * marker per event at its run's point (or the first point after it). `null` with
 * fewer than two points: there is no line to draw yet.
 */
export function coverageChart(overview: Pick<ProjectOverview, "coverage" | "events">): ChartPayload | null {
  const points = overview.coverage.points;
  if (points.length < 2) return null;
  const labels = pointLabels(points.map((p) => p.at));
  const byRun = new Map(points.map((p, i) => [p.run_id, i]));
  const events: string[][] = points.map(() => []);
  const markers: ChartMarker[] = [];
  for (const e of overview.events) {
    let index = byRun.get(e.run_id);
    if (index === undefined) {
      const at = Date.parse(e.at);
      const after = points.findIndex((p) => Date.parse(p.at) >= at);
      index = after === -1 ? points.length - 1 : after;
    }
    events[index].push(EVENT_LABEL[e.kind]);
    markers.push({ x: labels[index], label: EVENT_LABEL[e.kind], tone: EVENT_TONE[e.kind] });
  }
  return {
    kind: "line",
    title: "Commits described",
    xIndex: 0,
    yIndex: 1,
    columns: ["Scan", "Commits described", "Commits", "Symbols explained", "Events"],
    rows: points.map((p, i) => [labels[i], p.described_pct, p.commits, p.rationale_cards, events[i].join(", ")]),
    nullRows: 0,
    markers,
  };
}

/**
 * The 30-day bars of agent calls, one series per mode: MCP calls locally
 * (`/mcp/<slug>` is local-only), calls from linked portals in production
 * (`/api/v1` is production-only).
 */
export function agentChart(days: ProjectOverview["agents"]["days"], production: boolean): ChartPayload {
  const series = production ? "Calls from linked portals" : "MCP calls";
  return {
    kind: "bar",
    title: series,
    xIndex: 0,
    yIndex: 1,
    columns: ["Day", series],
    rows: days.map((d) => [DAY_UTC.format(new Date(`${d.day}T00:00:00Z`)), production ? d.agent : d.mcp]),
    nullRows: 0,
  };
}
