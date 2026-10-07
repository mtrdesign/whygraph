import { existsSync, readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChartPayload } from "../components/charts/chartSpec";
import { CHART_COLORS } from "../components/charts/chartTheme";
import { STORAGE_KEY, ThemeProvider, useTheme } from "../theme";
import { parseOklch } from "./oklch";

// Capture what ChartBlock hands to ECharts, without loading ECharts (canvas) itself.
const seen = vi.hoisted(() => ({ options: [] as Array<Record<string, unknown>> }));
vi.mock("echarts-for-react/esm/core", () => ({
  default: (props: { option: Record<string, unknown> }) => {
    seen.options.push(props.option);
    return <div data-testid="echart" />;
  },
}));
vi.mock("../components/charts/echarts", () => ({ default: {} }));

const { ChartBlock, buildOption } = await import("../components/charts/ChartBlock");

const css = readFileSync(resolve(import.meta.dirname, "../styles/theme.css"), "utf8");
const block = (selector: string) => {
  const start = css.indexOf(`\n${selector} {`);
  return css.slice(start, css.indexOf("\n}", start));
};
const token = (selector: string, name: string) =>
  new RegExp(`^\\s*${name}:\\s*([^;]+);`, "m").exec(block(selector))![1].trim();

const bar: ChartPayload = {
  kind: "bar",
  title: "Commits per author",
  xIndex: 0,
  yIndex: 1,
  columns: ["author", "commits"],
  rows: [
    ["alice", 12],
    ["bob", 7],
    ["carol", 3],
  ],
  nullRows: 0,
};

const stacked: ChartPayload = {
  ...bar,
  kind: "bar_stacked",
  stack: {
    seriesIndex: 1,
    seriesValues: ["feat", "fix"],
    xValues: ["Jun", "Jul"],
    cells: { Jun: { feat: 4, fix: 1 }, Jul: { feat: 2, fix: 5 } },
    filledCells: 0,
  },
};

/** Every string in an option, including those returned by its formatter. */
function strings(value: unknown, out: string[] = []): string[] {
  if (typeof value === "string") out.push(value);
  else if (Array.isArray(value)) value.forEach((v) => strings(v, out));
  else if (value && typeof value === "object") Object.values(value).forEach((v) => strings(v, out));
  return out;
}

describe("chart colour tables", () => {
  const HEX = /^#[0-9a-f]{6}$/;

  it.each(["light", "dark"] as const)("%s holds only resolved hex colours", (theme) => {
    const { palette, ...rest } = CHART_COLORS[theme];
    expect(palette).toHaveLength(6);
    expect([...palette, ...Object.values(rest)].every((c) => HEX.test(c))).toBe(true);
  });

  it("keeps the brand accent in slot 1 and distinct surfaces per theme", () => {
    expect(CHART_COLORS.light.palette[0]).not.toBe(CHART_COLORS.dark.palette[0]);
    expect(CHART_COLORS.light.surface).not.toBe(CHART_COLORS.dark.surface);
    expect(new Set(CHART_COLORS.light.palette).size).toBe(6);
    expect(new Set(CHART_COLORS.dark.palette).size).toBe(6);
  });

  it("stays the hex form of the theme.css tokens it mirrors", () => {
    const resolve = (selector: string, name: string) => {
      const value = token(selector, name);
      return parseOklch(value.startsWith("var(") ? token(selector, value.slice(4, -1)) : value);
    };
    for (const [theme, selector] of [
      ["light", ":root"],
      ["dark", ".dark"],
    ] as const) {
      const colors = CHART_COLORS[theme];
      expect(colors.surface).toBe(resolve(selector, "--muted"));
      expect(colors.tooltip).toBe(resolve(selector, "--popover"));
      expect(colors.muted).toBe(resolve(selector, "--muted-foreground"));
      expect(colors.fg).toBe(resolve(selector, "--foreground"));
    }
  });
});

describe("buildOption", () => {
  it.each(["light", "dark"] as const)("uses only resolved colours in %s", (theme) => {
    for (const payload of [bar, stacked, { ...bar, kind: "line" as const }]) {
      const option = buildOption(payload, CHART_COLORS[theme]);
      const all = strings(option);
      // The tooltip formatter builds DOM; its inline styles are colours too.
      const tooltip = (option.tooltip as { formatter: (p: unknown) => HTMLElement }).formatter([
        { axisValueLabel: "Jun", seriesName: "feat", seriesIndex: 1, value: 4 },
        { axisValueLabel: "Jun", seriesName: "fix", seriesIndex: 0, value: 1 },
      ]);
      all.push(tooltip.outerHTML);
      expect(all.some((s) => /oklch|var\(/i.test(s))).toBe(false);
    }
  });

  it("takes every colour from the table it is given", () => {
    const light = JSON.stringify(buildOption(stacked, CHART_COLORS.light));
    const dark = JSON.stringify(buildOption(stacked, CHART_COLORS.dark));
    expect(light).toContain(CHART_COLORS.light.palette[0]);
    expect(light).not.toContain(CHART_COLORS.dark.palette[0]);
    expect(dark).toContain(CHART_COLORS.dark.palette[0]);
    expect(dark).not.toContain(CHART_COLORS.light.palette[0]);
    expect(dark).not.toContain(CHART_COLORS.light.surface);
  });
});

describe("buildOption value formats", () => {
  type Fmt = (v: unknown) => string;
  const axisLabel = (o: Record<string, unknown>) =>
    (o.yAxis as { axisLabel: { formatter: (v: number) => string } }).axisLabel.formatter;
  const tooltipText = (o: Record<string, unknown>, value: number) =>
    (o.tooltip as { formatter: (p: unknown) => HTMLElement }).formatter([{ axisValueLabel: "Oct 1", value }])
      .textContent;
  const money: ChartPayload = { ...bar, rows: [["2026-10-01", 0.004], ["2026-10-02", 3.267], ["2026-10-03", 1234.4]] };

  it("formats axis, peak label and tooltip as money with usd", () => {
    const option = buildOption(money, CHART_COLORS.light, { valueFormat: "usd" });
    expect(axisLabel(option)(1234)).toBe("$1,234");
    expect(axisLabel(option)(2.5)).toBe("$2.50");
    expect(tooltipText(option, 0.004)).toContain("<$0.01");
    const data = (option.series as Array<{ data: Array<{ label?: { formatter: Fmt } } | number> }>)[0].data;
    const peak = data.find((d) => typeof d === "object" && d !== null && "label" in d) as { label: { formatter: Fmt } };
    expect(peak.label.formatter(undefined)).toBe("$1,234");
  });

  it("keeps plain numbers by default", () => {
    const option = buildOption(money, CHART_COLORS.light);
    expect(axisLabel(option)(1234)).toBe((1234).toLocaleString());
    expect(tooltipText(option, 12)).toContain("12");
    expect(tooltipText(option, 12)).not.toContain("$");
  });

  it("shows money in the Table view with usd", async () => {
    render(
      <ThemeProvider>
        <ChartBlock payload={money} valueFormat="usd" />
      </ThemeProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "table" }));
    expect(screen.getByText("$3.27")).toBeInTheDocument();
    expect(screen.getByText("2026-10-01")).toBeInTheDocument();
  });
});

describe("the chart move (components/charts)", () => {
  it("leaves no chart module under components/chat", () => {
    for (const name of ["ChartBlock.tsx", "chartSpec.ts", "chartTheme.ts", "echarts.ts"]) {
      expect(existsSync(resolve(import.meta.dirname, "../components/chat", name))).toBe(false);
      expect(existsSync(resolve(import.meta.dirname, "../components/charts", name))).toBe(true);
    }
  });

  it("keeps echarts behind the one chokepoint and out of the main bundle", () => {
    const src = resolve(import.meta.dirname, "..");
    const files = (dir: string): string[] =>
      readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
        e.isDirectory() ? files(resolve(dir, e.name)) : /\.tsx?$/.test(e.name) ? [resolve(dir, e.name)] : [],
      );
    const importers = files(src)
      .filter((f) => !f.includes("/test/"))
      .filter((f) => /from ["']echarts(\/|["'])/.test(readFileSync(f, "utf8")));
    expect(importers.map((f) => f.slice(src.length + 1))).toEqual(["components/charts/echarts.ts"]);
    // Only lazy imports reach ChartBlock (it pulls echarts in).
    const eager = files(src)
      .filter((f) => !f.includes("/test/"))
      .filter((f) => /^import (?!type )[^;]*from ["'][^"']*\/ChartBlock["']/m.test(readFileSync(f, "utf8")));
    expect(eager).toEqual([]);
  });
});

describe("ChartBlock follows the theme", () => {
  const root = document.documentElement;

  function Toggle() {
    const { setTheme } = useTheme();
    return (
      <>
        <button onClick={() => setTheme("dark")}>go dark</button>
        <button onClick={() => setTheme("light")}>go light</button>
      </>
    );
  }
  const mount = (children: ReactNode) => render(<ThemeProvider>{children}</ThemeProvider>);
  const lastOption = () => seen.options[seen.options.length - 1];
  const barColor = (option: Record<string, unknown>) =>
    (option.series as Array<{ itemStyle: { color: string } }>)[0].itemStyle.color;

  beforeEach(() => {
    seen.options.length = 0;
    localStorage.clear();
    root.classList.remove("dark");
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} })),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("rebuilds the option with the other palette on a live theme toggle", async () => {
    localStorage.setItem(STORAGE_KEY, "light");
    mount(
      <>
        <Toggle />
        <ChartBlock payload={bar} />
      </>,
    );
    expect(screen.getByTestId("echart")).toBeInTheDocument();
    expect(barColor(lastOption())).toBe(CHART_COLORS.light.palette[0]);

    await userEvent.click(screen.getByText("go dark"));
    expect(barColor(lastOption())).toBe(CHART_COLORS.dark.palette[0]);
    const grid = (o: Record<string, unknown>) =>
      (o.yAxis as { splitLine: { lineStyle: { color: string } } }).splitLine.lineStyle.color;
    expect(grid(lastOption())).toBe(CHART_COLORS.dark.grid);

    await userEvent.click(screen.getByText("go light"));
    expect(barColor(lastOption())).toBe(CHART_COLORS.light.palette[0]);
    expect(grid(lastOption())).toBe(CHART_COLORS.light.grid);
  });

  it("follows the system theme when it changes under it", async () => {
    let matches = false;
    const listeners = new Set<(e: MediaQueryListEvent) => void>();
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        get matches() {
          return matches;
        },
        addEventListener: (_: string, l: (e: MediaQueryListEvent) => void) => listeners.add(l),
        removeEventListener: (_: string, l: (e: MediaQueryListEvent) => void) => listeners.delete(l),
      })),
    );
    mount(<ChartBlock payload={bar} />);
    expect(barColor(lastOption())).toBe(CHART_COLORS.light.palette[0]);
    act(() => {
      matches = true;
      listeners.forEach((l) => l({ matches: true } as MediaQueryListEvent));
    });
    expect(barColor(lastOption())).toBe(CHART_COLORS.dark.palette[0]);
  });
});
