import ReactEChartsCore from "echarts-for-react/esm/core";
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { formatPct, formatUsd } from "@/lib/format";
import type { ChartMarker, ChartPayload } from "./chartSpec";
import { markerColor, useChartColors, type ChartColors } from "./chartTheme";
import echarts from "./echarts";

// The chart card: header, the plot, the mandatory Table twin, and PNG export.
//
// Colours come from `chartTheme.ts`: per-theme *resolved hex* tables, because the
// option object goes to a canvas that cannot parse OKLCH or `var()`. `buildOption`
// takes the table as an argument and ChartBlock rebuilds the option when the
// resolved theme changes.

const MAX_X_TICKS = 12;
/** Below this width (px) a time axis draws at most `NARROW_X_TICKS` labels. */
const NARROW_WIDTH = 480;
const NARROW_X_TICKS = 6;
const ROW_HEIGHT = 22;

// `EChartsOption` would have to come from the package root, and the tree-shaking
// guard greps for exactly that import. A type-only import is erased at build time,
// but a grep cannot tell the difference — so the option is typed structurally here
// and `echarts.ts` stays the only file that reaches into the library.
type Option = Record<string, unknown>;

/** How the measure reads: a plain count, a USD amount (the Usage & cost page), or a 0-100 percentage. */
export type ValueFormat = "number" | "usd" | "pct";

/** The label formatter for a {@link ValueFormat}. */
function valueFormatter(format: ValueFormat): (value: unknown) => string {
  if (format === "usd") return (value) => (typeof value === "number" ? formatUsd(value) : "—");
  if (format === "pct") return (value) => (typeof value === "number" ? formatPct(value) : "—");
  return formatValue;
}

/** The series that carries the markers; the tooltip lists its events, not its values. */
const MARKER_SERIES = "__markers";

const SEVERITY: Record<ChartMarker["tone"], number> = { info: 0, ok: 1, warn: 2, error: 3 };

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
function tooltipFormatter(
  colors: ChartColors,
  params: unknown,
  format: (value: unknown) => string = formatValue,
  markersAt?: Map<string, ChartMarker[]>,
): HTMLElement {
  const all = (Array.isArray(params) ? params : [params]) as Array<{
    axisValueLabel?: unknown;
    name?: unknown;
    seriesName?: unknown;
    seriesIndex?: number;
    value?: unknown;
  }>;
  const items = all.filter((item) => item.seriesName !== MARKER_SERIES);
  const category = String(all[0]?.axisValueLabel ?? all[0]?.name ?? "");

  const root = document.createElement("div");
  root.style.cssText = "font-size:12px;line-height:1.5;";

  const heading = document.createElement("div");
  heading.textContent = category;
  heading.style.cssText = `color:${colors.muted};margin-bottom:2px;`;
  root.append(heading);

  for (const item of items) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:6px;";

    if (items.length > 1) {
      const swatch = document.createElement("span");
      swatch.style.cssText =
        `width:8px;height:8px;border-radius:2px;flex:0 0 auto;` +
        `background:${colors.palette[(item.seriesIndex ?? 0) % colors.palette.length]};`;
      row.append(swatch);

      const label = document.createElement("span");
      label.textContent = String(item.seriesName ?? "");
      label.style.cssText = `color:${colors.muted};`;
      row.append(label);
    }

    const value = document.createElement("span");
    value.textContent = format(item.value);
    value.style.cssText = `color:${colors.fg};font-variant-numeric:tabular-nums;`;
    row.append(value);

    root.append(row);
  }

  // The events at this category, each behind a dot of its marker's colour.
  for (const marker of markersAt?.get(category) ?? []) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:6px;";
    const dot = document.createElement("span");
    dot.style.cssText =
      `width:8px;height:8px;border-radius:9999px;flex:0 0 auto;` + `background:${markerColor(colors, marker.tone)};`;
    row.append(dot);
    const label = document.createElement("span");
    label.textContent = marker.label;
    label.style.cssText = `color:${colors.fg};`;
    row.append(label);
    root.append(row);
  }
  return root;
}

/** How many category labels to skip so at most `MAX_X_TICKS` (6 on a narrow chart) are drawn. */
function tickInterval(count: number, width?: number): number {
  const max = width !== undefined && width < NARROW_WIDTH ? NARROW_X_TICKS : MAX_X_TICKS;
  return Math.max(0, Math.ceil(count / max) - 1);
}

/**
 * The whole visual specification, as one pure function.
 *
 * Kept pure and separate from the component so the spec is readable in one place
 * and a rendering question is answered by reading it rather than by tracing state.
 */
export function buildOption(
  payload: ChartPayload,
  colors: ChartColors,
  { valueFormat = "number", width }: { valueFormat?: ValueFormat; width?: number } = {},
): Option {
  const format = valueFormatter(valueFormat);
  const { palette } = colors;
  const mark = palette[0];
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
        color: palette[index % palette.length],
        borderColor: colors.surface,
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
        itemStyle: { color: mark },
        // A 10% wash rather than a distinct `area` kind — the recommended form for
        // a single-series trend, without a kind the model could mis-pick.
        areaStyle: { color: mark, opacity: 0.1 },
        showSymbol: false,
        emphasis: { itemStyle: { borderColor: colors.surface, borderWidth: 2 } },
        symbolSize: 8,
        // Direct labels selectively only: the last point, so a reader has one
        // anchored number without 200 of them fighting the line.
        endLabel: {
          show: true,
          color: colors.muted,
          fontSize: 11,
          // A function, not a `{@[1]}` template: label templates are painted onto
          // the canvas rather than parsed, so this is not a sink — but keeping
          // every formatter in this file a function means "is any formatter a
          // string?" stays a one-line answer.
          formatter: (params: { value?: unknown }) => format(params.value),
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
        itemStyle: { color: mark, borderRadius: radius },
        data: values.map((value, index) =>
          index === peak
            ? {
                value: value ?? null,
                label: {
                  show: true,
                  position: horizontal ? "right" : "top",
                  color: colors.muted,
                  fontSize: 11,
                  formatter: () => format(value),
                },
              }
            : (value ?? null),
        ),
      },
    ];
  }

  // Markers sit on the measured value of their category, as dots on an invisible
  // line (no extra ECharts component to register); the tooltip names them.
  const markersAt = new Map<string, ChartMarker[]>();
  if (!stacked && !horizontal) {
    const known = new Set(categories);
    for (const marker of payload.markers ?? []) {
      if (!known.has(marker.x)) continue;
      markersAt.set(marker.x, [...(markersAt.get(marker.x) ?? []), marker]);
    }
  }
  if (markersAt.size > 0) {
    series.push({
      name: MARKER_SERIES,
      type: "line",
      z: 3,
      symbol: "circle",
      symbolSize: 10,
      showSymbol: true,
      connectNulls: false,
      lineStyle: { opacity: 0 },
      data: categories.map((category, index) => {
        const here = markersAt.get(category);
        if (!here) return null;
        const worst = here.reduce((a, b) => (SEVERITY[b.tone] > SEVERITY[a.tone] ? b : a));
        return {
          value: rows[index]?.[yIndex] ?? null,
          itemStyle: { color: markerColor(colors, worst.tone), borderColor: colors.surface, borderWidth: 2 },
        };
      }),
    });
  }

  const categoryAxis: Option = {
    type: "category",
    data: categories,
    axisLabel: {
      color: colors.muted,
      fontSize: 11,
      // Never rotate — a rotated label is what `bar_h` exists to avoid. On a
      // horizontal chart every category label is shown, since long labels are the
      // reason that kind was chosen.
      rotate: 0,
      interval: horizontal ? 0 : tickInterval(categories.length, width),
      // Long labels on a narrow chart (a phone) drop out rather than overprint.
      hideOverlap: !horizontal,
    },
    axisLine: { lineStyle: { color: colors.grid } },
    axisTick: { show: false },
    splitLine: { show: false },
    // On a horizontal chart the first category would otherwise land at the bottom,
    // putting rank #1 furthest from the eye.
    ...(horizontal ? { inverse: true } : {}),
  };

  const valueAxis: Option = {
    type: "value",
    name: yLabel,
    nameTextStyle: { color: colors.muted, fontSize: 11 },
    splitNumber: 4,
    // A share reads on quarter ticks: 0 / 25 / 50 / 75 / 100 %.
    ...(valueFormat === "pct" ? { min: 0, max: 100, interval: 25 } : {}),
    axisLabel: {
      color: colors.muted,
      fontSize: 11,
      formatter: (value: number) =>
        valueFormat === "usd" ? formatUsd(value) : valueFormat === "pct" ? `${value}%` : value.toLocaleString(),
    },
    axisLine: { show: false },
    axisTick: { show: false },
    splitLine: { lineStyle: { color: colors.grid, width: 1, type: "solid" } },
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
          textStyle: { color: colors.muted, fontSize: 11 },
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
        type: isLine ? "line" : "shadow-sm",
        lineStyle: { color: colors.grid, width: 1 },
      },
      backgroundColor: colors.tooltip,
      borderColor: colors.grid,
      textStyle: { color: colors.fg, fontSize: 12 },
      extraCssText: "box-shadow:none;",
      formatter: (params: unknown) => tooltipFormatter(colors, params, format, markersAt),
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
function StatTile({ payload, valueFormat }: { payload: ChartPayload; valueFormat: ValueFormat }) {
  const value = payload.rows[0]?.[payload.yIndex];
  return (
    <div className="px-3 py-4">
      <div className="text-2xl text-foreground">{valueFormatter(valueFormat)(value)}</div>
      <div className="mt-0.5 text-xs text-muted-foreground">
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
function TableView({ payload, valueFormat }: { payload: ChartPayload; valueFormat: ValueFormat }) {
  return (
    <div className="max-h-72 overflow-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-muted">
          <tr>
            {payload.columns.map((column) => (
              <th
                key={column}
                className="border-b border-border px-2 py-1 text-left font-medium text-muted-foreground"
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
                  className={cn(
                    "px-2 py-1 text-foreground",
                    typeof row[cell] === "number" && "text-right tabular-nums",
                  )}
                >
                  {row[cell] === null || row[cell] === undefined
                    ? "—"
                    : valueFormat !== "number" && cell === payload.yIndex
                      ? valueFormatter(valueFormat)(row[cell])
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

export function ChartBlock({
  payload,
  valueFormat = "number",
}: {
  payload: ChartPayload;
  /** `usd` formats the measure as money, `pct` as a percentage (axis, labels, tooltip, Table view). */
  valueFormat?: ValueFormat;
}) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const instance = useRef<ReactEChartsCore>(null);
  const colors = useChartColors();
  // Rebuilt when the resolved theme flips, so a live toggle repaints the canvas.
  // The card's width, so a phone-width axis thins its labels (PH-9).
  const plot = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | undefined>(undefined);
  useEffect(() => {
    const node = plot.current;
    if (!node) return;
    setWidth(Math.round(node.getBoundingClientRect().width) || undefined);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width) || undefined));
    observer.observe(node);
    return () => observer.disconnect();
  }, [view]);
  const option = useMemo(
    () => buildOption(payload, colors, { valueFormat, width }),
    [payload, colors, valueFormat, width],
  );

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
      backgroundColor: colors.surface,
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
    <div className="my-1.5 overflow-hidden rounded-md border border-border bg-card">
      <div className="flex items-center gap-2 px-2.5 py-1.5">
        <div className="min-w-0 flex-1 truncate text-xs text-foreground">{payload.title}</div>
        {!statTile && (
          <>
            <div className="flex shrink-0 overflow-hidden rounded-sm border border-border">
              {(["chart", "table"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  aria-pressed={view === option}
                  onClick={() => setView(option)}
                  className={cn(
                    "px-1.5 py-0.5 text-[10px] capitalize transition-colors",
                    view === option
                      ? "bg-primary-soft text-primary-text"
                      : "text-muted-foreground hover:bg-accent",
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
                className="shrink-0 rounded-sm border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                PNG
              </button>
            )}
          </>
        )}
      </div>

      {statTile ? (
        <StatTile payload={payload} valueFormat={valueFormat} />
      ) : view === "table" ? (
        <TableView payload={payload} valueFormat={valueFormat} />
      ) : (
        <div
          ref={plot}
          role="img"
          aria-label={`${payload.title} — ${payload.kind} chart, ${categoryCount} categories. Use the Table toggle for the values.`}
        >
          <ReactEChartsCore
            ref={instance}
            echarts={echarts}
            option={option}
            style={{ width: "100%", height }}
            // A transcript reuses component positions as the user scrolls, and a
            // merged option would leak one chart's axis config into another's.
            notMerge
            lazyUpdate
          />
        </div>
      )}

      {captions.length > 0 && (
        <div className="space-y-0.5 px-2.5 pb-1.5 text-[10px] text-muted-foreground">
          {captions.map((caption) => (
            <div key={caption}>{caption}</div>
          ))}
        </div>
      )}
    </div>
  );
}
