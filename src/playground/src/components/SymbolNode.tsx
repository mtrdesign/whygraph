import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { clsx } from "clsx";
import { KindBadge } from "../lib/ui";

// A React Flow custom node for one symbol. Rendered at the server-supplied
// coordinates (no client-side layout). The focus node is visually emphasised.
export interface SymbolNodeData {
  qualified_name: string;
  name: string;
  kind: string;
  file_path: string;
  is_focus: boolean;
  [key: string]: unknown;
}

function SymbolNodeInner({ data }: NodeProps) {
  const d = data as SymbolNodeData;
  return (
    <div
      className={clsx(
        "min-w-[150px] max-w-[220px] rounded-lg border px-3 py-2 shadow-sm transition-colors",
        d.is_focus
          ? "border-accent2 bg-accent/20 ring-2 ring-accent/40"
          : "border-border bg-panel2 hover:border-accent2/60",
      )}
    >
      <Handle type="target" position={Position.Top} className="!bg-border" />
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium text-fg">{d.name}</span>
        <KindBadge kind={d.kind} />
      </div>
      <div className="mt-0.5 truncate text-[10px] text-muted">{d.file_path}</div>
      <Handle type="source" position={Position.Bottom} className="!bg-border" />
    </div>
  );
}

export const SymbolNode = memo(SymbolNodeInner);
