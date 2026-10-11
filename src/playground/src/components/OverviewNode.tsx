import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { FileIcon, FolderIcon } from "lucide-react";
import { cn } from "@/lib/utils";

// A LOD super-node: a directory or file, colored by rationale coverage.
export interface OverviewNodeData {
  label: string;
  kind: "directory" | "file";
  coverage: { analyzed: number; total: number; fraction: number };
  internal_edges: number;
  [key: string]: unknown;
}

function coverageColor(fraction: number, total: number): string {
  if (total === 0) return "bg-muted-foreground/30";
  if (fraction === 0) return "bg-muted-foreground/40";
  if (fraction < 0.5) return "bg-warning";
  if (fraction < 1) return "bg-success/60";
  return "bg-success";
}

function OverviewNodeInner({ data }: NodeProps) {
  const d = data as OverviewNodeData;
  const { analyzed, total, fraction } = d.coverage;
  const isDir = d.kind === "directory";
  return (
    <div
      className={cn(
        "min-w-[160px] max-w-[220px] rounded-lg border px-3 py-2 shadow-xs",
        isDir
          ? "border-border bg-muted hover:border-primary/60 cursor-pointer"
          : "border-border/60 bg-sidebar",
      )}
    >
      <Handle type="target" position={Position.Top} className="bg-border!" />
      <div className="flex items-center gap-2">
        {isDir ? (
          <FolderIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <FileIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
        )}
        <span className="truncate text-sm font-medium text-foreground">{d.label}</span>
      </div>
      <div
        className="mt-2 flex items-center gap-2"
        role="img"
        aria-label={`Explained: ${analyzed} of ${total} symbols`}
        title={`Explained: ${analyzed} of ${total} symbols`}
      >
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-track">
          <div
            className={cn("h-full", coverageColor(fraction, total))}
            style={{ width: `${total ? Math.max(fraction * 100, 3) : 0}%` }}
          />
        </div>
        {/* Visible words, not a bare "0/2" (EXC-6). */}
        <span className="shrink-0 text-[10px] text-muted-foreground" data-testid="overview-node-coverage">
          {analyzed} of {total} explained
        </span>
      </div>
      {d.internal_edges > 0 && (
        <div className="mt-1 text-[10px] text-muted-foreground">{d.internal_edges} internal</div>
      )}
      <Handle type="source" position={Position.Bottom} className="bg-border!" />
    </div>
  );
}

export const OverviewNode = memo(OverviewNodeInner);
