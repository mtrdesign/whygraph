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
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useExplorer } from "../store";
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
  const selectedQn = useExplorer((s) => s.selectedQn);
  const openNode = useExplorer((s) => s.openNode);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["ego", selectedQn],
    queryFn: () => api.ego(selectedQn!),
    enabled: !!selectedQn,
  });

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
        labelStyle: { fill: "#8b93a7", fontSize: 10 },
        labelBgStyle: { fill: "#12151c" },
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
      <div className="flex h-full items-center justify-center text-center text-muted">
        <div>
          <div className="text-lg font-medium text-fg">WhyGraph Explorer</div>
          <div className="mt-1 text-sm">
            Pick a symbol from the tree, or press{" "}
            <kbd className="rounded border border-border bg-panel2 px-1.5 py-0.5 text-xs">
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
      <div className="flex h-full items-center justify-center text-sm text-rose-400">
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
      proOptions={{ hideAttribution: true }}
    >
      <Background color="#242a36" gap={20} />
      <Controls className="!border-border !bg-panel2" showInteractive={false} />
    </ReactFlow>
  );
}
