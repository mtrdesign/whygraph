import { Suspense, lazy, useMemo } from "react";
import type { UsageDay } from "../../api";
import type { ChartPayload } from "../charts/chartSpec";
import { Skeleton } from "../ui/skeleton";

// ECharts stays out of the main bundle: the chart card loads on first use, from
// the same chunk the chat transcript's charts use.
const ChartBlock = lazy(() => import("../charts/ChartBlock").then((module) => ({ default: module.ChartBlock })));

/** `2026-10-07` -> `Oct 7` (UTC), short enough for a dozen axis ticks. */
function shortDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  return Number.isNaN(d.getTime())
    ? day
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** A daily series as the chart card's payload: one bar per UTC day. */
export function dailyCostPayload(series: UsageDay[], title: string): ChartPayload {
  return {
    kind: "bar",
    title,
    yLabel: "Cost (USD)",
    xIndex: 0,
    yIndex: 1,
    columns: ["day", "cost_usd", "calls"],
    rows: series.map((d) => [shortDay(d.day), d.cost_usd, d.calls]),
    nullRows: 0,
  };
}

/** Estimated cost per day, with the chart card's Table view and PNG export. */
export function DailyCostChart({ series, title = "Estimated cost per day" }: { series: UsageDay[]; title?: string }) {
  const payload = useMemo(() => dailyCostPayload(series, title), [series, title]);
  if (series.length === 0) return null;
  return (
    <div data-testid="usage-daily-chart">
      <Suspense fallback={<Skeleton className="h-[260px]" aria-label="Loading chart" />}>
        <ChartBlock payload={payload} valueFormat="usd" />
      </Suspense>
    </div>
  );
}
