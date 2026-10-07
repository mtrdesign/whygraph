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
