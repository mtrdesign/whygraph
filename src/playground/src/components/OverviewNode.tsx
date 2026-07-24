import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { clsx } from "clsx";

// A LOD super-node: a directory or file, colored by rationale coverage.
export interface OverviewNodeData {
  label: string;
  kind: "directory" | "file";
  coverage: { analyzed: number; total: number; fraction: number };
  internal_edges: number;
  [key: string]: unknown;
}

function coverageColor(fraction: number, total: number): string {
  if (total === 0) return "bg-slate-700";
  if (fraction === 0) return "bg-slate-600";
  if (fraction < 0.5) return "bg-amber-500";
  if (fraction < 1) return "bg-lime-500";
  return "bg-emerald-500";
}

function OverviewNodeInner({ data }: NodeProps) {
  const d = data as OverviewNodeData;
  const { analyzed, total, fraction } = d.coverage;
  const isDir = d.kind === "directory";
  return (
    <div
      className={clsx(
        "min-w-[160px] max-w-[220px] rounded-lg border px-3 py-2 shadow-sm",
        isDir
          ? "border-border bg-panel2 hover:border-accent2/60 cursor-pointer"
          : "border-border/60 bg-panel",
      )}
    >
      <Handle type="target" position={Position.Top} className="!bg-border" />
      <div className="flex items-center gap-2">
        <span className="text-xs">{isDir ? "📁" : "📄"}</span>
        <span className="truncate text-sm font-medium text-fg">{d.label}</span>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-panel">
          <div
            className={clsx("h-full", coverageColor(fraction, total))}
            style={{ width: `${total ? Math.max(fraction * 100, 3) : 0}%` }}
          />
        </div>
        <span className="text-[10px] text-muted">
          {analyzed}/{total}
        </span>
      </div>
      {d.internal_edges > 0 && (
        <div className="mt-1 text-[10px] text-muted">{d.internal_edges} internal</div>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-border" />
    </div>
  );
}

export const OverviewNode = memo(OverviewNodeInner);
