import { useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MarkerType,
  type Node,
  type Edge,
  type NodeMouseHandler,
} from "@xyflow/react";
import ELK from "elkjs/lib/elk.bundled.js";
import { useProjectQuery } from "../lib/project";
import { OverviewNode, type OverviewNodeData } from "./OverviewNode";
import { Loading } from "./Loading";
import { ErrorState } from "./state/ErrorState";
import { EmptyState } from "./state/EmptyState";
import { useTheme } from "../theme";

// The Phase-2 LOD overview and landing view: directory super-nodes with weighted,
// directional lifted edges and coverage coloring. Clicking a directory expands it
// (server re-lifts for the new expansion state). Layout runs client-side with elk
// (the node count is bounded by the expansion state, so it stays fast).

const nodeTypes = { overview: OverviewNode };
const elk = new ELK();
const NODE_W = 190;
const NODE_H = 72;

interface OverviewApiNode {
  id: string;
  kind: "directory" | "file";
  label: string;
  path: string;
  coverage: { analyzed: number; total: number; fraction: number };
  internal_edges: number;
}
interface OverviewApiEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
  weight: number;
}

async function layout(
  apiNodes: OverviewApiNode[],
  apiEdges: OverviewApiEdge[],
): Promise<Record<string, { x: number; y: number }>> {
  const graph = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "DOWN",
      "elk.spacing.nodeNode": "40",
      "elk.layered.spacing.nodeNodeBetweenLayers": "70",
    },
    children: apiNodes.map((n) => ({ id: n.id, width: NODE_W, height: NODE_H })),
    edges: apiEdges.map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  };
  const res = await elk.layout(graph);
  const pos: Record<string, { x: number; y: number }> = {};
  for (const c of res.children ?? []) pos[c.id] = { x: c.x ?? 0, y: c.y ?? 0 };
  return pos;
}

export function Overview() {
  const { resolvedTheme } = useTheme();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});

  const expandedParam = useMemo(() => [...expanded].sort().join(","), [expanded]);
  const { data, isLoading, isError, error, refetch } = useProjectQuery(["overview", expandedParam], (api) =>
    api.overview(expandedParam),
  );

  useEffect(() => {
    if (!data) return;
    let alive = true;
    layout(data.nodes, data.edges).then((pos) => {
      if (alive) setPositions(pos);
    });
    return () => {
      alive = false;
    };
  }, [data]);

  const nodes = useMemo<Node[]>(
    () =>
      (data?.nodes ?? []).map((n) => ({
        id: n.id,
        type: "overview",
        position: positions[n.id] ?? { x: 0, y: 0 },
        data: n as unknown as OverviewNodeData,
      })),
    [data, positions],
  );

  const edges = useMemo<Edge[]>(
    () =>
      (data?.edges ?? []).map((e) => {
        const stroke = e.kind === "imports" ? "var(--destructive)" : "var(--primary-text)";
        return {
          id: e.id,
          source: e.source,
          target: e.target,
          label: e.weight > 1 ? String(e.weight) : undefined,
          style: { stroke, strokeWidth: Math.min(1 + e.weight / 3, 4) },
          labelStyle: { fill: "var(--muted-foreground)", fontSize: 10 },
          labelBgStyle: { fill: "var(--card)" },
          markerEnd: { type: MarkerType.ArrowClosed, color: stroke },
        };
      }),
    [data],
  );

  const onNodeClick: NodeMouseHandler = (_, node) => {
    const d = node.data as unknown as OverviewNodeData & { path: string };
    if (d.kind !== "directory") return;
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(d.path) ? next.delete(d.path) : next.add(d.path);
      return next;
    });
  };

  // Hold the canvas until the first elk layout lands: <ReactFlow fitView> fits once,
  // and doing it with every node still stacked at (0, 0) zooms to the maximum.
  const laidOut = !data || data.nodes.length === 0 || Object.keys(positions).length > 0;
  if (isLoading || !laidOut)
    return (
      <div className="flex h-full items-center justify-center">
        <Loading label="Loading overview…" />
      </div>
    );
  if (isError)
    return (
      <div className="flex h-full items-center justify-center p-4">
        <ErrorState
          title="Couldn't load the overview"
          error={error}
          onRetry={() => void refetch()}
          className="w-full max-w-md"
        />
      </div>
    );
  if (data && data.nodes.length === 0)
    return (
      <EmptyState
        className="h-full"
        title="Nothing indexed yet"
        description="The code index has no symbols to draw. Run a scan from the project Overview to build it."
      />
    );

  return (
    <div className="h-full">
      <div className="absolute left-1/2 top-3 z-10 -translate-x-1/2 whitespace-nowrap rounded-full border border-border bg-muted/80 px-3 py-1 text-xs text-muted-foreground backdrop-blur-sm">
        Overview - click a directory to expand · coverage colored
      </div>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={onNodeClick}
        onlyRenderVisibleElements
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.1}
        maxZoom={2}
        colorMode={resolvedTheme}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="var(--border)" gap={20} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
