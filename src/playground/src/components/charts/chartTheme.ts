import { useTheme, type ResolvedTheme } from "../../theme";

// ECharts draws to a canvas and zrender does not parse OKLCH (nor `var()`), so the
// chart cannot read the CSS tokens the rest of the UI uses. Instead each theme has
// a table of *resolved hex* colours, and ChartBlock rebuilds its option from the
// table that matches the resolved theme. No OKLCH or `var()` string may appear in
// an option - `chartTheme.test.ts` guards that, and guards that the surface and
// text colours here are still the hex form of the `theme.css` tokens they mirror.
//
// SURFACE is the assistant bubble (`--muted`), because that is what the chart card
// sits on. The categorical palette was run through the dataviz validator against
// its own theme's surface (adjacent-pair CVD separation, normal-vision floor, 3:1
// contrast): the dark table is the original palette, the light table re-steps the
// hues that dropped below 3:1 on the light surface (yellow, mainly). Slot 1 is the
// app's indigo in both. Slot order is fixed, so a series keeps its colour across a
// re-render, a reload, and a theme switch.

export interface ChartColors {
  /** The chart card surface - also the stacked-bar seam and the PNG background. */
  surface: string;
  /** The tooltip surface, one step off `surface`. */
  tooltip: string;
  /** Gridlines and the axis rule: 1px solid, never dashed. */
  grid: string;
  /** Axis ticks and captions; never a mark colour. */
  muted: string;
  /** Values and tooltip text. */
  fg: string;
  /** Six fixed categorical slots; slot 0 is the brand accent. */
  palette: readonly string[];
}

export const CHART_COLORS: Record<ResolvedTheme, ChartColors> = {
  light: {
    surface: "#f2f2ef", // --muted
    tooltip: "#fcfcfa", // --popover
    grid: "#d9d7d4",
    muted: "#626369", // --muted-foreground
    fg: "#18181b", // --foreground
    palette: ["#5959e8", "#d95926", "#199e70", "#b07800", "#d55181", "#008300"],
  },
  dark: {
    surface: "#18181b", // --muted
    tooltip: "#131316", // --popover
    grid: "#2d2d31",
    muted: "#a4a4ab", // --muted-foreground
    fg: "#ebebee", // --foreground
    palette: ["#6366f1", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"],
  },
};

/** The chart colour table for the theme that is actually painted. */
export function useChartColors(): ChartColors {
  return CHART_COLORS[useTheme().resolvedTheme];
}
