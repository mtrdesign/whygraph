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
import { useQuery } from "@tanstack/react-query";
import ELK from "elkjs/lib/elk.bundled.js";
import { api } from "../api";
import { OverviewNode, type OverviewNodeData } from "./OverviewNode";
import { Spinner } from "../lib/ui";

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
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});

  const expandedParam = useMemo(() => [...expanded].sort().join(","), [expanded]);
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["overview", expandedParam],
    queryFn: () => api.overview(expandedParam),
  });

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
      (data?.edges ?? []).map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        label: e.weight > 1 ? String(e.weight) : undefined,
        style: {
          stroke: e.kind === "imports" ? "#fb7185" : "#818cf8",
          strokeWidth: Math.min(1 + e.weight / 3, 4),
        },
        labelStyle: { fill: "#8b93a7", fontSize: 10 },
        labelBgStyle: { fill: "#12151c" },
        markerEnd: { type: MarkerType.ArrowClosed },
      })),
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

  if (isLoading)
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner label="Loading overview…" />
      </div>
    );
  if (isError)
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-muted">
        <div className="text-sm text-rose-400">{(error as Error).message}</div>
        <div className="text-xs">
          Run <code className="text-fg">whygraph scan</code> to build the index.
        </div>
      </div>
    );

  return (
    <div className="h-full">
      <div className="absolute left-1/2 top-3 z-10 -translate-x-1/2 rounded-full border border-border bg-panel2/80 px-3 py-1 text-xs text-muted backdrop-blur">
        Overview — click a directory to expand · coverage colored
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
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#242a36" gap={20} />
        <Controls className="!border-border !bg-panel2" showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
