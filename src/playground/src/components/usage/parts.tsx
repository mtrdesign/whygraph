import { useState, type FormEvent, type ReactNode } from "react";
import { DownloadIcon } from "lucide-react";
import type { UsageCsv, UsageSplit } from "../../api";
import { authMessage } from "../../lib/authErrors";
import { saveBlob } from "../../lib/download";
import { formatPct, formatUsd } from "../../lib/format";
import {
  RANGE_PRESETS,
  addDays,
  matchPreset,
  presetRange,
  type RangePreset,
  type UsageRange,
} from "../../lib/usageRange";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Field, nativeSelectClass } from "../portal/Field";
import { cn } from "@/lib/utils";

// Small shared pieces of the Usage & cost pages (the org page, the member
// drill-downs, and the project / account sections that reuse them).

/** A titled card, the portal's section style. */
export function UsageSection({
  title,
  description,
  actions,
  children,
  className,
  testId,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <section
      data-testid={testId}
      className={cn("flex flex-col gap-4 rounded-xl border border-border bg-card p-5", className)}
    >
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold">{title}</h2>
          {description && <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

/** One headline figure. */
export function StatTile({
  label,
  value,
  detail,
  testId,
}: {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  testId?: string;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-lg border border-border px-3 py-2.5" data-testid={testId}>
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-xl font-semibold tabular-nums tracking-tight">{value}</span>
      {detail && <span className="text-xs text-muted-foreground">{detail}</span>}
    </div>
  );
}

/** The label every money figure carries: WhyGraph estimates, the provider invoices. */
export function EstimatedNote({ className }: { className?: string }) {
  return (
    <p className={cn("text-xs text-muted-foreground", className)} data-testid="estimated-note">
      All costs are <strong className="font-medium text-foreground">estimated</strong>: the provider's own figure
      where it reports one (OpenRouter), otherwise the tokens it reported times the price table. Your provider's
      bill is the source of truth.
    </p>
  );
}

/** "N calls had no price": their tokens count, their cost does not. */
export function UnpricedNote({ calls, budgets = false }: { calls: number; budgets?: boolean }) {
  if (calls <= 0) return null;
  return (
    <p className="text-xs text-warning" data-testid="unpriced-note">
      {calls.toLocaleString("en-US")} {calls === 1 ? "call" : "calls"} had no price and{" "}
      {budgets ? "are not counted toward budgets" : "are not in the cost figures"}. Add a price on the Prices tab
      to count calls like {calls === 1 ? "it" : "them"} from now on.
    </p>
  );
}

/** Interactive (chat, generate, agent calls, backfill) vs. scans started, as one line. */
export function SplitLine({ split }: { split: UsageSplit }) {
  return (
    <p className="text-xs text-muted-foreground" data-testid="usage-split">
      <span className="text-foreground">Interactive</span> {formatUsd(split.interactive.cost_usd)} (
      {split.interactive.calls.toLocaleString("en-US")} calls) ·{" "}
      <span className="text-foreground">Scans</span> {formatUsd(split.scans.cost_usd)} (
      {split.scans.calls.toLocaleString("en-US")} calls)
    </p>
  );
}

/**
 * Spend against a budget: a bar that turns to the warning colour at 75% and to
 * the destructive colour at 100%. `pct` is the server's (one decimal).
 */
export function SpendBar({ pct, label }: { pct: number | null; label?: string }) {
  if (pct === null) return null;
  const width = Math.max(0, Math.min(100, pct));
  return (
    <div
      role="progressbar"
      aria-label={label ?? "Spent of budget"}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(width)}
      aria-valuetext={formatPct(pct)}
      className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
    >
      <div
        className={cn(
          "h-full rounded-full transition-all",
          pct >= 100 ? "bg-destructive" : pct >= 75 ? "bg-warning" : "bg-primary",
        )}
        style={{ width: `${width}%` }}
      />
    </div>
  );
}

/**
 * A CSV download (a fetch, not a link: the API needs a header). Says so when the
 * server cut the export at 50,000 rows.
 */
export function CsvButton({
  download,
  label = "Download CSV",
  testId = "usage-csv",
}: {
  download: () => Promise<UsageCsv>;
  label?: string;
  testId?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const run = async () => {
    setBusy(true);
    setError(null);
    setTruncated(false);
    try {
      const csv = await download();
      saveBlob(csv.blob, csv.filename);
      setTruncated(csv.truncated);
    } catch (err) {
      setError(authMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col items-end gap-1">
      <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void run()} data-testid={testId}>
        <DownloadIcon />
        {label}
      </Button>
      {truncated && (
        <p className="max-w-xs text-right text-xs text-warning" role="status" data-testid={`${testId}-truncated`}>
          The export stopped at 50,000 rows. Narrow the date range or add a filter to get the rest.
        </p>
      )}
      {error && (
        <p className="max-w-xs text-right text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * The date range of a usage view: presets, or a custom From / To. The URL keeps
 * the API's convention (`to` exclusive); the To field shows the last day included.
 */
export function RangePicker({
  range,
  onChange,
}: {
  range: Partial<UsageRange>;
  onChange: (range: Partial<UsageRange>) => void;
}) {
  const preset = matchPreset(range);
  const [custom, setCustom] = useState(preset === "custom");
  const [draft, setDraft] = useState({
    from: range.from ?? "",
    to: range.to ? addDays(range.to, -1) : "",
  });
  const choose = (value: string) => {
    if (value === "custom") {
      const current = range.from && range.to ? range : presetRange("this_month");
      setDraft({ from: current.from ?? "", to: current.to ? addDays(current.to, -1) : "" });
      setCustom(true);
      return;
    }
    setCustom(false);
    // This month is the server's default: keep the address clean.
    onChange(value === "this_month" ? {} : presetRange(value as RangePreset));
  };
  const apply = (e: FormEvent) => {
    e.preventDefault();
    if (!draft.from || !draft.to) return;
    onChange({ from: draft.from, to: addDays(draft.to, 1) });
  };
  return (
    <form onSubmit={apply} className="flex flex-wrap items-end gap-2" data-testid="range-picker">
      <Field label="Range" className="w-40">
        {(p) => (
          <select
            {...p}
            className={nativeSelectClass}
            value={custom ? "custom" : preset}
            onChange={(e) => choose(e.target.value)}
          >
            {RANGE_PRESETS.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
            <option value="custom">Custom</option>
          </select>
        )}
      </Field>
      {custom && (
        <>
          <Field label="From" className="w-40">
            {(p) => <Input {...p} type="date" value={draft.from} onChange={(e) => setDraft({ ...draft, from: e.target.value })} />}
          </Field>
          <Field label="To" className="w-40">
            {(p) => <Input {...p} type="date" value={draft.to} onChange={(e) => setDraft({ ...draft, to: e.target.value })} />}
          </Field>
          <Button type="submit" variant="outline" disabled={!draft.from || !draft.to}>
            Apply
          </Button>
        </>
      )}
    </form>
  );
}
