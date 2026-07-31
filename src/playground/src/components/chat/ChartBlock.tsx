import ReactEChartsCore from "echarts-for-react/lib/core";
import { useRef, useState } from "react";
import { clsx } from "clsx";
import type { ChartPayload } from "./chartSpec";
import echarts from "./echarts";

// The chart card: header, the plot, the mandatory Table twin, and PNG export.
//
// Colours here are raw hex on purpose — they go inside the ECharts `option`
// object, which Tailwind cannot reach. Every one is a `tailwind.config.js` token
// and the data colour was chosen by running a palette validator against this exact
// surface, not by eye. `accent2` (#818cf8) is deliberately absent: it failed the
// dark lightness band, which is why links and marks do not share a colour here.
const SURFACE = "#171b24"; // panel2 — the assistant bubble, so the chart surface
const PANEL = "#12151c"; // panel — the tooltip, one step back from the surface
const BORDER = "#242a36"; // gridlines and the axis rule, 1px solid, never dashed
const MUTED = "#8b93a7"; // axis ticks and captions, never a mark colour
const FG = "#e6e9f0"; // values and tooltip text

// Six fixed slots for stack segments, in this order. Slot 1 is the app's accent.
// Validated as a palette against SURFACE on the *adjacent* pairlist — a stack only
// ever places touching segments side by side, which is what makes six slots
// available where a scatter plot would be capped at three. Slot order is fixed and
// assignment comes from the server's `seriesValues`, so a segment keeps its colour
// across a re-render, a reload, and a scroll-past.
const PALETTE = ["#6366f1", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"];
const MARK = PALETTE[0];

const MAX_X_TICKS = 12;
const ROW_HEIGHT = 22;

// `EChartsOption` would have to come from the package root, and the tree-shaking
// guard greps for exactly that import. A type-only import is erased at build time,
// but a grep cannot tell the difference — so the option is typed structurally here
// and `echarts.ts` stays the only file that reaches into the library.
type Option = Record<string, unknown>;

/** Compact a number for a label: 1,284 / 12.9K / 3.4M. */
export function formatValue(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  if (magnitude >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (magnitude >= 10_000) return `${(value / 1000).toFixed(1)}K`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function slugify(title: string): string {
  return (
    title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "chart"
  );
}

/**
 * The tooltip content, built as a DOM element with `textContent`.
 *
 * ECharts renders a formatter *string* as HTML — that is how people add coloured
 * dots — and our labels are repo content: a commit subject, a symbol name, an
 * author login. A string formatter would put `<img src=x onerror=…>` from a commit
 * message into an HTML sink. Returning a built element means no HTML is ever
 * parsed, so escaping cannot be got wrong.
 *
 * The swatch colour comes from our own palette by index, never from the params, so
 * even the one styled attribute here is not data-derived.
 */
function tooltipFormatter(params: unknown): HTMLElement {
  const items = (Array.isArray(params) ? params : [params]) as Array<{
    axisValueLabel?: unknown;
    name?: unknown;
    seriesName?: unknown;
    seriesIndex?: number;
    value?: unknown;
  }>;

  const root = document.createElement("div");
  root.style.cssText = "font-size:12px;line-height:1.5;";

  const heading = document.createElement("div");
  heading.textContent = String(items[0]?.axisValueLabel ?? items[0]?.name ?? "");
  heading.style.cssText = `color:${MUTED};margin-bottom:2px;`;
  root.append(heading);

  for (const item of items) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:6px;";

    if (items.length > 1) {
      const swatch = document.createElement("span");
      swatch.style.cssText =
        `width:8px;height:8px;border-radius:2px;flex:0 0 auto;` +
        `background:${PALETTE[(item.seriesIndex ?? 0) % PALETTE.length]};`;
      row.append(swatch);

      const label = document.createElement("span");
      label.textContent = String(item.seriesName ?? "");
      label.style.cssText = `color:${MUTED};`;
      row.append(label);
    }

    const value = document.createElement("span");
    value.textContent = formatValue(item.value);
    value.style.cssText = `color:${FG};font-variant-numeric:tabular-nums;`;
    row.append(value);

    root.append(row);
  }
  return root;
}

/** How many category labels to skip so at most `MAX_X_TICKS` are drawn. */
function tickInterval(count: number): number {
  return Math.max(0, Math.ceil(count / MAX_X_TICKS) - 1);
}

/**
 * The whole visual specification, as one pure function.
 *
 * Kept pure and separate from the component so the spec is readable in one place
 * and a rendering question is answered by reading it rather than by tracing state.
 */
export function buildOption(payload: ChartPayload): Option {
  const { kind, rows, xIndex, yIndex, yLabel, stack } = payload;
  const horizontal = kind === "bar_h" || kind === "bar_h_stacked";
  const isLine = kind === "line";
  const stacked = !!stack;

  const categories = stacked
    ? stack.xValues
    : rows.map((row) => String(row[xIndex] ?? ""));

  const radius: [number, number, number, number] = horizontal
    ? [0, 4, 4, 0]
    : [4, 4, 0, 0];

  let series: Option[];
  if (stacked) {
    // Push in `seriesValues` order, so the largest segment sits at the axis and
    // the eye compares it against a straight edge. The seam is a 2px
    // surface-coloured border — the one place a border on a bar is correct, and
    // the secondary encoding the palette's adjacent-pair margin leans on.
    series = stack.seriesValues.map((name, index) => ({
      name,
      type: "bar",
      stack: "total",
      barMaxWidth: 24,
      itemStyle: {
        color: PALETTE[index % PALETTE.length],
        borderColor: SURFACE,
        borderWidth: 2,
        // Rounded on the outermost segment only. Rounding every segment reads as
        // separate floating bars rather than one total.
        borderRadius: index === stack.seriesValues.length - 1 ? radius : 0,
      },
      data: stack.xValues.map((x) => stack.cells[x]?.[name] ?? 0),
    }));
  } else if (isLine) {
    series = [
      {
        type: "line",
        data: rows.map((row) => row[yIndex] ?? null),
        lineStyle: { width: 2, cap: "round", join: "round" },
        itemStyle: { color: MARK },
        // A 10% wash rather than a distinct `area` kind — the recommended form for
        // a single-series trend, without a kind the model could mis-pick.
        areaStyle: { color: MARK, opacity: 0.1 },
        showSymbol: false,
        emphasis: { itemStyle: { borderColor: SURFACE, borderWidth: 2 } },
        symbolSize: 8,
        // Direct labels selectively only: the last point, so a reader has one
        // anchored number without 200 of them fighting the line.
        endLabel: {
          show: true,
          color: MUTED,
          fontSize: 11,
          // A function, not a `{@[1]}` template: label templates are painted onto
          // the canvas rather than parsed, so this is not a sink — but keeping
          // every formatter in this file a function means "is any formatter a
          // string?" stays a one-line answer.
          formatter: (params: { value?: unknown }) => formatValue(params.value),
        },
      },
    ];
  } else {
    // The single largest bar carries a label. Per-datum config rather than a
    // `markPoint`, which would need a component this bundle does not register.
    const values = rows.map((row) => row[yIndex]);
    let peak = -1;
    let best = -Infinity;
    values.forEach((value, index) => {
      if (typeof value === "number" && value > best) {
        best = value;
        peak = index;
      }
    });
    series = [
      {
        type: "bar",
        barMaxWidth: 24,
        barCategoryGap: "20%",
        itemStyle: { color: MARK, borderRadius: radius },
        data: values.map((value, index) =>
          index === peak
            ? {
                value: value ?? null,
                label: {
                  show: true,
                  position: horizontal ? "right" : "top",
                  color: MUTED,
                  fontSize: 11,
                  formatter: () => formatValue(value),
                },
              }
            : (value ?? null),
        ),
      },
    ];
  }

  const categoryAxis: Option = {
    type: "category",
    data: categories,
    axisLabel: {
      color: MUTED,
      fontSize: 11,
      // Never rotate — a rotated label is what `bar_h` exists to avoid. On a
      // horizontal chart every category label is shown, since long labels are the
      // reason that kind was chosen.
      rotate: 0,
      interval: horizontal ? 0 : tickInterval(categories.length),
    },
    axisLine: { lineStyle: { color: BORDER } },
    axisTick: { show: false },
    splitLine: { show: false },
    // On a horizontal chart the first category would otherwise land at the bottom,
    // putting rank #1 furthest from the eye.
    ...(horizontal ? { inverse: true } : {}),
  };

  const valueAxis: Option = {
    type: "value",
    name: yLabel,
    nameTextStyle: { color: MUTED, fontSize: 11 },
    splitNumber: 4,
    axisLabel: {
      color: MUTED,
      fontSize: 11,
      formatter: (value: number) => value.toLocaleString(),
    },
    axisLine: { show: false },
    axisTick: { show: false },
    splitLine: { lineStyle: { color: BORDER, width: 1, type: "solid" } },
  };

  return {
    animation: true,
    backgroundColor: "transparent",
    grid: {
      top: stacked ? 28 : 8,
      right: 16,
      bottom: 4,
      left: 4,
      // Constrains the grid *including its axis labels* to this box, so a fixed
      // container cannot clip a long file path. `containLabel: true` was the
      // ECharts 5 spelling; in 6 it is legacy and warns on every render unless
      // `LegacyGridContainLabel` is registered. Measured equivalent: the longest
      // category label lands at the same x under either.
      outerBounds: { top: stacked ? 28 : 8, right: 16, bottom: 4, left: 4 },
    },
    legend: stacked
      ? {
          show: true,
          top: 0,
          left: 0,
          itemWidth: 8,
          itemHeight: 8,
          itemGap: 12,
          icon: "roundRect",
          textStyle: { color: MUTED, fontSize: 11 },
          data: stack.seriesValues,
        }
      : // One measure has nothing to key, and the card header already carries the
        // title as real DOM text.
        { show: false },
    tooltip: {
      trigger: "axis",
      axisPointer: {
        // On bars the shadow makes the hit area the whole category band including
        // the 2px gap; on a line a crosshair reads better.
        type: isLine ? "line" : "shadow",
        lineStyle: { color: BORDER, width: 1 },
      },
      backgroundColor: PANEL,
      borderColor: BORDER,
      textStyle: { color: FG, fontSize: 12 },
      extraCssText: "box-shadow:none;",
      formatter: tooltipFormatter,
    },
    xAxis: horizontal ? valueAxis : categoryAxis,
    yAxis: horizontal ? categoryAxis : valueAxis,
    series,
  };
}

/**
 * The 1-row-by-1-value form: the number *is* the chart.
 *
 * A one-bar chart is a cataloged anti-pattern, and this case is mostly prevented
 * upstream — a producer mints no `chart_ref` below two rows — but the fallback
 * stays for anything that slips through. No ECharts instance is created, and no
 * download button is offered: there is nothing to save that the sentence beside it
 * does not already say. Proportional figures, not `tabular-nums`: equal-width
 * digits make a large standalone number look loose.
 */
function StatTile({ payload }: { payload: ChartPayload }) {
  const value = payload.rows[0]?.[payload.yIndex];
  return (
    <div className="px-3 py-4">
      <div className="text-2xl text-fg">{formatValue(value)}</div>
      <div className="mt-0.5 text-xs text-muted">
        {payload.yLabel ?? payload.columns[payload.yIndex]}
      </div>
    </div>
  );
}

/**
 * The accessible twin, and not optional.
 *
 * Under the canvas renderer the chart puts no text in the DOM — it is not
 * selectable and not screen-readable. This table is the only representation of the
 * numbers that is, and it is drawn from the same rows the chart plots, so the two
 * cannot disagree. If it is ever dropped, the renderer choice has to be revisited.
 */
function TableView({ payload }: { payload: ChartPayload }) {
  return (
    <div className="max-h-72 overflow-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-panel2">
          <tr>
            {payload.columns.map((column) => (
              <th
                key={column}
                className="border-b border-border px-2 py-1 text-left font-medium text-muted"
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {payload.rows.map((row, index) => (
            <tr key={index} className="border-b border-border/40">
              {payload.columns.map((column, cell) => (
                <td
                  key={column}
                  className={clsx(
                    "px-2 py-1 text-fg",
                    typeof row[cell] === "number" && "text-right tabular-nums",
                  )}
                >
                  {row[cell] === null || row[cell] === undefined
                    ? "—"
                    : String(row[cell])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ChartBlock({ payload }: { payload: ChartPayload }) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const instance = useRef<ReactEChartsCore>(null);

  const statTile = payload.rows.length === 1 && !payload.stack;
  const horizontal = payload.kind === "bar_h" || payload.kind === "bar_h_stacked";
  const categoryCount = payload.stack
    ? payload.stack.xValues.length
    : payload.rows.length;
  // A horizontal chart's categories stack vertically, so its height has to grow
  // with them — 20 file paths in 220px is a smear, whatever `containLabel` does.
  const height = horizontal
    ? Math.min(520, Math.max(220, categoryCount * ROW_HEIGHT + 48))
    : 220;

  const download = () => {
    const chart = instance.current?.getEchartsInstance();
    if (!chart) return;
    const url = chart.getDataURL({
      type: "png",
      pixelRatio: 2,
      // Opaque, not transparent: a transparent PNG pasted into a light document
      // is unreadable.
      backgroundColor: SURFACE,
    });
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${slugify(payload.title)}-${payload.rows.length}-rows.png`;
    anchor.click();
  };

  const captions: string[] = [];
  if (payload.nullRows > 0) {
    captions.push(
      `${payload.nullRows} row${payload.nullRows === 1 ? "" : "s"} had no value and ${
        payload.nullRows === 1 ? "is" : "are"
      } not plotted.`,
    );
  }
  if (payload.stack && payload.stack.filledCells > 0) {
    // The zero-fill is correct — an absent GROUP BY group means zero — but it must
    // never be silent, or a reader comparing the chart to the table cannot explain
    // why the table has fewer rows than the chart has cells.
    captions.push(
      `${payload.stack.filledCells} combination${
        payload.stack.filledCells === 1 ? "" : "s"
      } had no rows and ${payload.stack.filledCells === 1 ? "is" : "are"} shown as zero.`,
    );
  }

  return (
    <div className="my-1.5 overflow-hidden rounded-md border border-border bg-panel2/60">
      <div className="flex items-center gap-2 px-2.5 py-1.5">
        <div className="min-w-0 flex-1 truncate text-xs text-fg">{payload.title}</div>
        {!statTile && (
          <>
            <div className="flex shrink-0 overflow-hidden rounded border border-border">
              {(["chart", "table"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  aria-pressed={view === option}
                  onClick={() => setView(option)}
                  className={clsx(
                    "px-1.5 py-0.5 text-[10px] capitalize transition-colors",
                    view === option
                      ? "bg-accent/20 text-fg"
                      : "text-muted hover:bg-panel2",
                  )}
                >
                  {option}
                </button>
              ))}
            </div>
            {view === "chart" && (
              <button
                type="button"
                onClick={download}
                title="Save as PNG"
                className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:bg-panel2 hover:text-fg"
              >
                PNG
              </button>
            )}
          </>
        )}
      </div>

      {statTile ? (
        <StatTile payload={payload} />
      ) : view === "table" ? (
        <TableView payload={payload} />
      ) : (
        <div
          role="img"
          aria-label={`${payload.title} — ${payload.kind} chart, ${categoryCount} categories. Use the Table toggle for the values.`}
        >
          <ReactEChartsCore
            ref={instance}
            echarts={echarts}
            option={buildOption(payload)}
            style={{ width: "100%", height }}
            // A transcript reuses component positions as the user scrolls, and a
            // merged option would leak one chart's axis config into another's.
            notMerge
            lazyUpdate
          />
        </div>
      )}

      {captions.length > 0 && (
        <div className="space-y-0.5 px-2.5 pb-1.5 text-[10px] text-muted">
          {captions.map((caption) => (
            <div key={caption}>{caption}</div>
          ))}
        </div>
      )}
    </div>
  );
}
