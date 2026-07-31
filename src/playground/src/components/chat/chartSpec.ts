import type { ToolActivity } from "./ToolCallCard";

// The parse boundary, and the correlation step the two-step chart design needs.
//
// `render_chart` returns the *directive* — kind, title, resolved column indices —
// but not the rows: the producing stats call already delivered those, so echoing
// them would double a 200-row payload for nothing. That means the rows have to be
// found again here, by matching `chart_ref` against the sibling activities of the
// same turn.
//
// This works identically live and on replay, because `turns.ts` groups activities
// per turn in both paths. It is the one piece of the split design that could have
// been awkward, and isn't.
//
// Nothing here throws. A tool result is a string that arrived over the network and
// may be truncated mid-array by the server's own result cap, so `JSON.parse` is
// the load-bearing `try` — a chart that fails to parse must render as no chart
// beside numbers that are still correct, never as a crashed transcript.

export interface ChartStack {
  seriesIndex: number;
  /** Stack order and colour-slot order: descending by total, ties lexicographic. */
  seriesValues: string[];
  /** The x axis, in the query's own row order. */
  xValues: string[];
  /** cells[xValue][seriesValue] -> number. Dense: absent combinations are 0. */
  cells: Record<string, Record<string, number>>;
  /** How many cells the server filled, for the caption. Never silent. */
  filledCells: number;
}

export interface ChartPayload {
  kind: "line" | "bar" | "bar_h" | "bar_stacked" | "bar_h_stacked";
  title: string;
  yLabel?: string;
  xIndex: number;
  yIndex: number;
  columns: string[];
  rows: unknown[][];
  nullRows: number;
  /**
   * Stacked kinds only. The server sends the densified grid, so the frontend
   * never pivots and the chart and the Table view cannot disagree.
   */
  stack?: ChartStack;
}

const KINDS = new Set(["line", "bar", "bar_h", "bar_stacked", "bar_h_stacked"]);
const STACKED = new Set(["bar_stacked", "bar_h_stacked"]);

/** Parse a tool result, returning null rather than throwing on anything odd. */
function parseResult(result: string | undefined): Record<string, unknown> | null {
  if (!result) return null;
  try {
    const parsed: unknown = JSON.parse(result);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** The `chart_ref` a stats result carried, if it carried one. */
function refOf(activity: ToolActivity): string | null {
  if (activity.running) return null;
  const parsed = parseResult(activity.result);
  const ref = parsed?.chart_ref;
  return typeof ref === "string" ? ref : null;
}

function readStack(chart: Record<string, unknown>): ChartStack | null {
  const seriesIndex = chart.series_index;
  const seriesValues = chart.series_values;
  const xValues = chart.x_values;
  const cells = chart.cells;
  if (
    typeof seriesIndex !== "number" ||
    !Array.isArray(seriesValues) ||
    !Array.isArray(xValues) ||
    !cells ||
    typeof cells !== "object"
  ) {
    return null;
  }
  return {
    seriesIndex,
    seriesValues: seriesValues.map(String),
    xValues: xValues.map(String),
    cells: cells as Record<string, Record<string, number>>,
    filledCells: typeof chart.filled_cells === "number" ? chart.filled_cells : 0,
  };
}

/**
 * Build a chart payload from a `render_chart` activity.
 *
 * @param activity - the `render_chart` card itself.
 * @param turnActivities - every activity in the same turn, searched for the
 *   producing stats call that minted the same `chart_ref`.
 * @returns the payload, or `null` when the activity is not a chart, is still
 *   running, errored, is unparseable, has no matching producer in this turn, or
 *   resolves to indices outside the producer's columns.
 */
export function parseChart(
  activity: ToolActivity,
  turnActivities: ToolActivity[],
): ChartPayload | null {
  if (activity.name !== "render_chart" || activity.running) return null;

  const result = parseResult(activity.result);
  // `error` is the normal failure path, not an exception: a bad directive leaves
  // the producer's numbers on screen and tells the model how to fix the call.
  if (!result || "error" in result) return null;

  const ref = result.chart_ref;
  const chart = result.chart;
  if (typeof ref !== "string" || !chart || typeof chart !== "object") return null;
  const directive = chart as Record<string, unknown>;

  const kind = directive.kind;
  const title = directive.title;
  const xIndex = directive.x_index;
  const yIndex = directive.y_index;
  if (
    typeof kind !== "string" ||
    !KINDS.has(kind) ||
    typeof title !== "string" ||
    typeof xIndex !== "number" ||
    typeof yIndex !== "number"
  ) {
    return null;
  }

  // The correlation step: find the producer that minted this ref, in this turn.
  const producer = turnActivities.find(
    (candidate) => candidate !== activity && refOf(candidate) === ref,
  );
  if (!producer) return null;
  const produced = parseResult(producer.result);
  const columns = produced?.columns;
  const rows = produced?.rows;
  if (!Array.isArray(columns) || !Array.isArray(rows)) return null;

  // The server resolved these indices, but the rows travelled separately — so
  // re-check the bound rather than trusting two payloads to agree.
  if (xIndex < 0 || yIndex < 0 || xIndex >= columns.length || yIndex >= columns.length) {
    return null;
  }

  let stack: ChartStack | undefined;
  if (STACKED.has(kind)) {
    const read = readStack(directive);
    // A stacked kind with no grid is not drawable as anything else — a bar chart
    // of one series would answer a different question.
    if (!read) return null;
    stack = read;
  }

  return {
    kind: kind as ChartPayload["kind"],
    title,
    yLabel: typeof directive.y_label === "string" ? directive.y_label : undefined,
    xIndex,
    yIndex,
    columns: columns.map(String),
    rows: rows as unknown[][],
    nullRows: typeof directive.null_rows === "number" ? directive.null_rows : 0,
    stack,
  };
}
