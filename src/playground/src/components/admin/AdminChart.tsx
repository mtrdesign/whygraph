import { Suspense, lazy } from "react";
import type { ChartPayload } from "../charts/chartSpec";
import type { ValueFormat } from "../charts/ChartBlock";
import { Skeleton } from "../ui/skeleton";

// ECharts stays out of the main bundle: the card loads on first use, from the
// same chunk the chat transcript's and the Usage page's charts use.
const ChartBlock = lazy(() => import("../charts/ChartBlock").then((module) => ({ default: module.ChartBlock })));

/** A lazy `ChartBlock` in its `Suspense`, or the sentence `empty` when there is nothing to draw. */
export function AdminChart({
  payload,
  valueFormat,
  empty,
  testId,
}: {
  payload: ChartPayload | null;
  valueFormat: ValueFormat;
  /** What an empty series says (a chart with no rows draws a sentence, not an axis). */
  empty: string;
  testId: string;
}) {
  if (!payload || payload.rows.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid={`${testId}-empty`}>
        {empty}
      </p>
    );
  }
  return (
    <div data-testid={testId}>
      <Suspense fallback={<Skeleton className="h-[260px]" aria-label="Loading chart" />}>
        <ChartBlock payload={payload} valueFormat={valueFormat} />
      </Suspense>
    </div>
  );
}
