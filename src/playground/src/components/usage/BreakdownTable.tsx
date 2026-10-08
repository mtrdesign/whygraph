import type { ReactNode } from "react";
import type { UsageGroup, UsageGroupRow } from "../../api";
import { formatTokens, formatUsd } from "../../lib/format";

const KEY_HEADINGS: Record<UsageGroup, string> = {
  project: "Project",
  member: "Member",
  task: "Task",
  model: "Model",
  source: "Source",
  machine: "Machine",
  day: "Day",
};

/**
 * One `group=` breakdown as a table: calls, tokens, the interactive / scans split
 * and the estimated cost, costliest first (the server's order). `link` turns a
 * row's label into a link (a filtered Calls view, a member drill-down) or returns
 * `null` to leave it as text.
 */
export function BreakdownTable({
  group,
  rows,
  link,
  limit,
  compact = false,
}: {
  group: UsageGroup;
  rows: UsageGroupRow[];
  link?: (row: UsageGroupRow) => ReactNode | null;
  /** Show only the first rows (an overview's "top 5"). */
  limit?: number;
  /** Fewer columns, for a narrow card. */
  compact?: boolean;
}) {
  const shown = limit ? rows.slice(0, limit) : rows;
  if (shown.length === 0) {
    return <p className="text-sm text-muted-foreground">No LLM calls in this range.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs" data-testid={`breakdown-${group}`}>
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1.5 pr-3 font-medium">{KEY_HEADINGS[group]}</th>
            <th className="py-1.5 pr-3 text-right font-medium">Calls</th>
            {!compact && <th className="py-1.5 pr-3 text-right font-medium">Tokens in</th>}
            {!compact && <th className="py-1.5 pr-3 text-right font-medium">Tokens out</th>}
            {!compact && <th className="py-1.5 pr-3 text-right font-medium">Interactive</th>}
            {!compact && <th className="py-1.5 pr-3 text-right font-medium">Scans</th>}
            {group === "member" && !compact && <th className="py-1.5 pr-3 font-medium">Top project</th>}
            <th className="py-1.5 text-right font-medium">Est. cost</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {shown.map((row, i) => (
            <tr key={`${row.key ?? "null"}:${row.label}:${i}`} data-testid={`breakdown-row-${row.key ?? row.label}`}>
              <td className="max-w-[18rem] truncate py-1.5 pr-3" title={row.label}>
                {link?.(row) ?? row.label}
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">{row.calls.toLocaleString("en-US")}</td>
              {!compact && <td className="py-1.5 pr-3 text-right tabular-nums">{formatTokens(row.input_tokens)}</td>}
              {!compact && <td className="py-1.5 pr-3 text-right tabular-nums">{formatTokens(row.output_tokens)}</td>}
              {!compact && (
                <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                  {formatUsd(row.interactive.cost_usd)}
                </td>
              )}
              {!compact && (
                <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                  {formatUsd(row.scans.cost_usd)}
                </td>
              )}
              {group === "member" && !compact && (
                <td className="max-w-[12rem] truncate py-1.5 pr-3 text-muted-foreground">
                  {row.top_project ? `${row.top_project.name} (${formatUsd(row.top_project.cost_usd)})` : "-"}
                </td>
              )}
              <td className="py-1.5 text-right tabular-nums">
                {formatUsd(row.cost_usd)}
                {row.unpriced_calls > 0 && (
                  <span className="block text-[11px] text-warning">{row.unpriced_calls} unpriced</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
