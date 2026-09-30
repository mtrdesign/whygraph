import { useMemo } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MarkerType,
  type Node,
  type Edge,
  type NodeMouseHandler,
} from "@xyflow/react";
import { useExplorerSearch, useOpenNode } from "../lib/nav";
import { useProjectQuery } from "../lib/project";
import { useTheme } from "../theme";
import { SymbolNode, type SymbolNodeData } from "./SymbolNode";
import { Spinner } from "../lib/ui";

// The center canvas: the one-hop ego graph of the selected symbol. Coordinates
// come from the server (§0 rendering strategy) — the client only pans/zooms and
// never runs a force simulation, the direct fix for the old viewer's jank.

const nodeTypes = { symbol: SymbolNode };

const EDGE_COLOR: Record<string, string> = {
  calls: "#818cf8",
  imports: "#fb7185",
  contains: "#64748b",
};

export function GraphCanvas() {
  const { resolvedTheme } = useTheme();
  const selectedQn = useExplorerSearch().node;
  const openNode = useOpenNode();

  const { data, isLoading, isError, error } = useProjectQuery(
    ["ego", selectedQn],
    (api) => api.ego(selectedQn!),
    { enabled: !!selectedQn },
  );

  const nodes = useMemo<Node[]>(
    () =>
      (data?.nodes ?? []).map((n) => ({
        id: n.id,
        type: "symbol",
        position: n.position,
        data: n.data as unknown as SymbolNodeData,
      })),
    [data],
  );

  const edges = useMemo<Edge[]>(
    () =>
      (data?.edges ?? []).map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        label: e.kind,
        animated: e.kind === "calls",
        style: { stroke: EDGE_COLOR[e.kind] ?? "#64748b" },
        labelStyle: { fill: "var(--muted-foreground)", fontSize: 10 },
        labelBgStyle: { fill: "var(--card)" },
        markerEnd: { type: MarkerType.ArrowClosed, color: EDGE_COLOR[e.kind] ?? "#64748b" },
      })),
    [data],
  );

  const onNodeClick: NodeMouseHandler = (_, node) => {
    const d = node.data as unknown as SymbolNodeData;
    if (!d.is_focus) openNode(d.qualified_name, d.file_path);
  };

  if (!selectedQn)
    return (
      <div className="flex h-full items-center justify-center text-center text-muted-foreground">
        <div>
          <div className="text-lg font-medium text-fg">WhyGraph Explorer</div>
          <div className="mt-1 text-sm">
            Pick a symbol from the tree, or press{" "}
            <kbd className="rounded-sm border border-border bg-panel2 px-1.5 py-0.5 text-xs">
              ⌘K
            </kbd>{" "}
            to search.
          </div>
        </div>
      </div>
    );

  if (isLoading)
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner label="Loading graph…" />
      </div>
    );

  if (isError)
    return (
      <div className="flex h-full items-center justify-center text-sm text-rose-600 dark:text-rose-400">
        {(error as Error).message}
      </div>
    );

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodeClick={onNodeClick}
      onlyRenderVisibleElements
      fitView
      fitViewOptions={{ padding: 0.3 }}
      minZoom={0.2}
      maxZoom={2}
      colorMode={resolvedTheme}
      proOptions={{ hideAttribution: true }}
    >
      <Background color="var(--border)" gap={20} />
      <Controls className="border-border! bg-panel2!" showInteractive={false} />
    </ReactFlow>
  );
}
