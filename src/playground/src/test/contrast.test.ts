import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { CHART_COLORS } from "../components/charts/chartTheme";
import { parseOklch } from "./oklch";

const css = readFileSync(resolve(import.meta.dirname, "../styles/theme.css"), "utf8");

function block(selector: string) {
  const start = css.indexOf(`\n${selector} {`);
  return css.slice(start, css.indexOf("\n}", start));
}

/** The hex of a token, following `var(--other)` aliases within the block. */
function colour(selector: string, name: string): string {
  const match = new RegExp(`^\\s*${name}:\\s*([^;]+);`, "m").exec(block(selector));
  if (!match) throw new Error(`${name} missing in ${selector}`);
  const value = match[1].trim();
  return value.startsWith("var(") ? colour(selector, value.slice(4, -1)) : parseOklch(value);
}

function luminance(hex: string) {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function ratio(a: string, b: string) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const THEMES = [
  ["light", ":root"],
  ["dark", ".dark"],
] as const;

describe.each(THEMES)("%s theme contrast", (theme, selector) => {
  const c = (name: string) => colour(selector, name);

  it.each(["success", "warning", "destructive", "info"])(
    "%s text holds 4.5:1 on its soft background and on --card",
    (tone) => {
      expect(ratio(c(`--${tone}`), c(`--${tone}-soft`))).toBeGreaterThanOrEqual(4.5);
      expect(ratio(c(`--${tone}`), c("--card"))).toBeGreaterThanOrEqual(4.5);
    },
  );

  it("keeps --primary-text readable on --primary-soft and --card", () => {
    expect(ratio(c("--primary-text"), c("--primary-soft"))).toBeGreaterThanOrEqual(4.5);
    expect(ratio(c("--primary-text"), c("--card"))).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps the disabled primary button text readable on --muted", () => {
    expect(ratio(c("--muted-foreground"), c("--muted"))).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps --input a 3:1 boundary on every surface it borders", () => {
    for (const surface of ["--background", "--card", "--popover", "--muted"]) {
      expect(ratio(c("--input"), c(surface))).toBeGreaterThanOrEqual(3);
    }
  });

  it("keeps the six chart series at 3:1 on the chart surface", () => {
    for (const series of CHART_COLORS[theme].palette) {
      expect(ratio(series, c("--card"))).toBeGreaterThanOrEqual(3);
    }
  });
});
