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
import { Loading } from "./Loading";
import { Kbd } from "./ui/kbd";

// The center canvas: the one-hop ego graph of the selected symbol. Coordinates
// come from the server (§0 rendering strategy) — the client only pans/zooms and
// never runs a force simulation, the direct fix for the old viewer's jank.

const nodeTypes = { symbol: SymbolNode };

// `var()` is only ever used on SVG strokes and fills (xyflow paints edges and
// markers as SVG), so the edges follow the theme without a rebuild.
const EDGE_COLOR: Record<string, string> = {
  calls: "var(--primary-text)",
  imports: "var(--destructive)",
  contains: "var(--muted-foreground)",
};
const EDGE_FALLBACK = "var(--muted-foreground)";

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
        style: { stroke: EDGE_COLOR[e.kind] ?? EDGE_FALLBACK },
        labelStyle: { fill: "var(--muted-foreground)", fontSize: 10 },
        labelBgStyle: { fill: "var(--card)" },
        markerEnd: { type: MarkerType.ArrowClosed, color: EDGE_COLOR[e.kind] ?? EDGE_FALLBACK },
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
          <div className="text-lg font-medium text-foreground">WhyGraph Explorer</div>
          <div className="mt-1 text-sm">
            Pick a symbol from the tree, or press{" "}
            <Kbd>⌘K</Kbd>{" "}
            to search.
          </div>
        </div>
      </div>
    );

  if (isLoading)
    return (
      <div className="flex h-full items-center justify-center">
        <Loading label="Loading graph…" />
      </div>
    );

  if (isError)
    return (
      <div className="flex h-full items-center justify-center text-sm text-destructive">
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
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}
