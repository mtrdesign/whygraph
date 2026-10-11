import type { ReactNode } from "react";
import type { UsageGroup, UsageGroupRow } from "../../api";
import { formatNumber, formatTokens, formatUsd } from "../../lib/format";
import { ResponsiveTable, type Column } from "../layout/ResponsiveTable";

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
 * `null` to leave it as text. Below `sm` each row stacks (PH-5).
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
  const muted = "text-muted-foreground";
  const columns: Column<UsageGroupRow>[] = [
    {
      key: "key",
      header: KEY_HEADINGS[group],
      primary: true,
      cell: (row) => <span title={row.label}>{link?.(row) ?? row.label}</span>,
    },
    { key: "calls", header: "Calls", align: "right", cell: (row) => formatNumber(row.calls) },
  ];
  if (!compact) {
    columns.push(
      { key: "in", header: "Tokens in", align: "right", cell: (row) => formatTokens(row.input_tokens) },
      { key: "out", header: "Tokens out", align: "right", cell: (row) => formatTokens(row.output_tokens) },
      {
        key: "interactive",
        header: "Interactive",
        align: "right",
        cell: (row) => <span className={muted}>{formatUsd(row.interactive.cost_usd)}</span>,
      },
      {
        key: "scans",
        header: "Scans",
        align: "right",
        cell: (row) => <span className={muted}>{formatUsd(row.scans.cost_usd)}</span>,
      },
    );
    if (group === "member") {
      columns.push({
        key: "top",
        header: "Top project",
        cell: (row) => (
          <span className={muted}>
            {row.top_project ? `${row.top_project.name} (${formatUsd(row.top_project.cost_usd)})` : "-"}
          </span>
        ),
      });
    }
  }
  columns.push({
    key: "cost",
    header: "Est. cost",
    align: "right",
    cell: (row) => (
      <>
        {formatUsd(row.cost_usd)}
        {row.unpriced_calls > 0 && <span className="block text-[11px] text-warning">{row.unpriced_calls} unpriced</span>}
      </>
    ),
  });
  return (
    <div data-testid={`breakdown-${group}`}>
      <ResponsiveTable
        columns={columns}
        rows={shown}
        rowKey={(row) => `${row.key ?? "null"}:${row.label}`}
        rowTestId={(row) => `breakdown-row-${row.key ?? row.label}`}
      />
    </div>
  );
}
