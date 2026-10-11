import type { ReactNode } from "react";
import { cn } from "cn";

export interface Column<T> {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  /** The row's title in the stacked (phone) layout. */
  primary?: boolean;
  /** Hide this column in the table layout below the breakpoint. */
  hideBelow?: "sm" | "md";
  align?: "left" | "right";
}

const HIDE = { sm: "max-sm:hidden", md: "max-md:hidden" } as const;

/**
 * A table from `sm` up, a stacked list below it: the `primary` column is each
 * row's title and the others are label / value pairs, so no key column hides
 * behind an inner scroll.
 */
export function ResponsiveTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  rowTestId,
  empty,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  /** A test id for each row of the table layout (the stacked list repeats the rows, so it carries none). */
  rowTestId?: (row: T) => string;
  empty?: ReactNode;
}) {
  if (rows.length === 0 && empty) return <>{empty}</>;
  const primary = columns.find((c) => c.primary) ?? columns[0];
  const others = columns.filter((c) => c !== primary);
  const clickable = onRowClick ? "cursor-pointer hover:bg-muted/50" : "";
  return (
    <>
      <div className="hidden overflow-hidden rounded-xl border border-border bg-card sm:block shadow-card">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              {columns.map((c) => (
                <th
                  key={c.key}
                  scope="col"
                  className={cn(
                    "px-3 py-2 font-medium",
                    c.align === "right" && "text-right",
                    c.hideBelow && HIDE[c.hideBelow],
                  )}
                >
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={rowKey(row)}
                data-testid={rowTestId?.(row)}
                className={cn("border-b border-border last:border-0", clickable)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
              >
                {columns.map((c) => (
                  <td
                    key={c.key}
                    className={cn(
                      "px-3 py-2 align-top",
                      c.align === "right" && "text-right tabular-nums",
                      c.hideBelow && HIDE[c.hideBelow],
                    )}
                  >
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="flex flex-col gap-2 sm:hidden">
        {rows.map((row) => (
          <li
            key={rowKey(row)}
            className={cn("rounded-xl border border-border bg-card p-3 text-sm", clickable)}
            onClick={onRowClick ? () => onRowClick(row) : undefined}
          >
            <div className="min-w-0 font-medium break-words">{primary.cell(row)}</div>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
              {others.map((c) => {
                const value = c.cell(row);
                // An empty cell is left out: no label without a value.
                if (value === null || value === undefined || value === "" || value === false) return null;
                return (
                  <div key={c.key} className="contents">
                    <dt className="text-xs text-muted-foreground">{c.header}</dt>
                    <dd className="min-w-0 break-words">{value}</dd>
                  </div>
                );
              })}
            </dl>
          </li>
        ))}
      </ul>
    </>
  );
}
