// Money and token figures, formatted one way everywhere (Usage & cost, the scan
// estimate, the charts).

const CENTS = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const DOLLARS = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

/**
 * A USD amount: cents under $10 (`$3.27`), whole dollars from $10 (`$1,234`), and
 * `<$0.01` for a cost that rounds to nothing but is not zero. `null` (nothing
 * reported) is `-`.
 */
export function formatUsd(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  const magnitude = Math.abs(value);
  if (magnitude > 0 && magnitude < 0.01) return value < 0 ? "-<$0.01" : "<$0.01";
  return magnitude < 10 ? CENTS.format(value) : DOLLARS.format(value);
}

/** A token count: `1_234_567` -> `1.2M`, `12_345` -> `12.3k`; small numbers stay exact. `null` is `-`. */
export function formatTokens(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  const n = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (n >= 1_000_000) return `${sign}${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1_000) return `${sign}${(n / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${sign}${Math.round(n)}`;
}

/** A percentage as the API sends it (one decimal), or `-`. */
export function formatPct(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  return `${value.toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
}

type When = string | number | Date | null | undefined;

function toDate(value: When): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

// The viewer's locale, one shape per helper.
const DATE = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
});
const CLOCK = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const NUMBER = new Intl.NumberFormat(undefined);

/** `9 Oct 2026` (the viewer's locale); `-` for a missing or invalid value. */
export function formatDate(value: When): string {
  const d = toDate(value);
  return d ? DATE.format(d) : "-";
}

/** The date plus hour and minute; `-` for a missing or invalid value. */
export function formatDateTime(value: When): string {
  const d = toDate(value);
  return d ? DATE_TIME.format(d) : "-";
}

/** `14:02` (the viewer's locale); `-` for a missing or invalid value. */
export function formatClock(value: When): string {
  const d = toDate(value);
  return d ? CLOCK.format(d) : "-";
}

/**
 * A date part in UTC, for the usage pages that bucket by UTC day and month
 * (`{ month: "short", day: "numeric" }` -> `Oct 7`). `locale` pins the spelling the caller wants.
 */
export function formatUtc(value: When, options: Intl.DateTimeFormatOptions, locale = "en-US"): string {
  const d = toDate(value);
  return d ? new Intl.DateTimeFormat(locale, { ...options, timeZone: "UTC" }).format(d) : "-";
}

/** A whole-number-friendly count in the viewer's locale (`1,234`); `-` when not a number. */
export function formatNumber(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  return NUMBER.format(value);
}

/** `2026-09-30T12:00:00+00:00` -> `12 min ago`. Falls back to the date beyond a month. */
export function timeAgo(iso: string | null, now: number = Date.now()): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const sec = Math.max(0, Math.round((now - then) / 1000));
  if (sec < 60) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hours = Math.round(min / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 31) return `${days} d ago`;
  return formatDate(then);
}

/** `3 min ago`, or `-` for a missing or invalid value. */
export function formatRelative(value: When, now: number = Date.now()): string {
  const d = toDate(value);
  return (d && timeAgo(d.toISOString(), now)) || "-";
}
