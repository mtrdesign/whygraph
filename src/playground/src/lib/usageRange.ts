// Date ranges for Usage & cost. The server counts calendar days and months in UTC,
// `from` inclusive and `to` exclusive, so every range here is a pair of UTC
// `YYYY-MM-DD` strings in that convention.

import { formatDateTimeUtc, formatDateUtc, formatUtc } from "./format";

export interface UsageRange {
  from: string;
  to: string;
}

export type RangePreset = "this_month" | "last_month" | "7d" | "30d" | "90d" | "365d";

export const RANGE_PRESETS: { value: RangePreset; label: string }[] = [
  { value: "this_month", label: "This month" },
  { value: "last_month", label: "Last month" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
  { value: "365d", label: "Last 12 months" },
];

const DAY_MS = 86_400_000;

/** The UTC calendar day of `date`, as `YYYY-MM-DD`. */
export function utcDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** `day` moved by `n` days (UTC). */
export function addDays(day: string, n: number): string {
  return utcDay(new Date(Date.parse(`${day}T00:00:00Z`) + n * DAY_MS));
}

/** The UTC calendar month `offset` months from `now`'s (0 = this month, -1 = last month). */
export function monthRange(offset: number, now: Date = new Date()): UsageRange {
  const y = now.getUTCFullYear();
  const m = now.getUTCMonth() + offset;
  return {
    from: utcDay(new Date(Date.UTC(y, m, 1))),
    to: utcDay(new Date(Date.UTC(y, m + 1, 1))),
  };
}

/** The range a preset names, as of `now`; the "last N days" presets include today. */
export function presetRange(preset: RangePreset, now: Date = new Date()): UsageRange {
  if (preset === "this_month") return monthRange(0, now);
  if (preset === "last_month") return monthRange(-1, now);
  const days = Number.parseInt(preset, 10);
  const tomorrow = addDays(utcDay(now), 1);
  return { from: addDays(tomorrow, -days), to: tomorrow };
}

/** The preset `range` is, or `"custom"`; no range at all is the server's default, this month. */
export function matchPreset(range: Partial<UsageRange>, now: Date = new Date()): RangePreset | "custom" {
  if (!range.from && !range.to) return "this_month";
  const hit = RANGE_PRESETS.find(({ value }) => {
    const r = presetRange(value, now);
    return r.from === range.from && r.to === range.to;
  });
  return hit?.value ?? "custom";
}

/** `2026-10` -> `October 2026` (the viewer's locale). */
export function formatMonth(month: string): string {
  const d = new Date(`${month}-01T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return month;
  return formatUtc(d, { month: "long", year: "numeric" });
}

/**
 * A reset instant as the budget UI says it: `formatDateTime`'s viewer-locale shape in
 * UTC (`Nov 1, 2026, 12:00 AM UTC`), the same shape as the alerts' "When".
 */
export function formatResetsAt(iso: string): string {
  return toDate(iso) ? formatDateTimeUtc(iso) : iso;
}

/** `Nov 1` (or `1 Nov`): the day a month's budgets reset (UTC), for a banner. */
export function formatResetsOn(iso: string): string {
  return toDate(iso) ? formatUtc(iso, { day: "numeric", month: "short" }) : iso;
}

/** A range for a caption: `Oct 1, 2026 - Oct 31, 2026` (the exclusive end shown as the last day). */
export function formatRange(range: UsageRange): string {
  const show = (day: string) => formatDateUtc(`${day}T00:00:00Z`);
  const last = addDays(range.to, -1);
  return last === range.from ? show(range.from) : `${show(range.from)} - ${show(last)}`;
}

function toDate(iso: string): Date | null {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}
