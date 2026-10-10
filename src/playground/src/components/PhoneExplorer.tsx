import { useState } from "react";
import { ArrowLeftIcon } from "lucide-react";
import { useExplorerSearch } from "../lib/nav";
import { DetailPanel } from "./DetailPanel";
import { GraphCanvas } from "./GraphCanvas";
import { Overview } from "./Overview";
import { Tree } from "./Tree";
import { Button } from "./ui/button";
import { Tabs, TabsList, TabsTrigger } from "./ui/tabs";

type PaneKey = "tree" | "graph" | "details";

const PANES: { key: PaneKey; label: string }[] = [
  { key: "tree", label: "Tree" },
  { key: "graph", label: "Graph" },
  { key: "details", label: "Details" },
];

/**
 * The Explorer below `md` (M2f-3 PH-1): the tree, the graph and the detail panel are one
 * full-height pane at a time, picked by scrollable tabs. Selecting a node (in the tree, the
 * graph, Cmd-K or a deep link) opens Details; "Back to graph" returns. What is selected stays in
 * the URL (`?node&file`), so a link opens the same place on a desktop. Only the active pane is
 * mounted, so the graph always lays out at the pane's real size.
 */
export function PhoneExplorer() {
  const { node } = useExplorerSearch();
  const [pane, setPane] = useState<PaneKey>(node ? "details" : "tree");
  const [seen, setSeen] = useState(node);
  // A new selection (not the first render, not a clear) opens Details.
  if (seen !== node) {
    setSeen(node);
    if (node) setPane("details");
  }

  return (
    <Tabs value={pane} onValueChange={(v) => setPane(v as PaneKey)} className="min-h-0 flex-1 gap-0" data-testid="phone-explorer">
      <TabsList variant="scrollable" aria-label="Explorer panes" className="shrink-0 rounded-none px-4">
        {PANES.map((p) => (
          <TabsTrigger key={p.key} value={p.key} className="text-sm">
            {p.label}
          </TabsTrigger>
        ))}
      </TabsList>
      <div className="relative min-h-0 flex-1 bg-background" data-testid={`phone-pane-${pane}`}>
        {pane === "tree" && <Tree />}
        {pane === "graph" && (node ? <GraphCanvas /> : <Overview />)}
        {pane === "details" && (
          <div className="flex h-full flex-col">
            {node && (
              <div className="shrink-0 border-b border-border px-2 py-1">
                <Button variant="ghost" size="sm" onClick={() => setPane("graph")}>
                  <ArrowLeftIcon data-icon="inline-start" />
                  Back to graph
                </Button>
              </div>
            )}
            <div className="min-h-0 flex-1">
              <DetailPanel />
            </div>
          </div>
        )}
      </div>
    </Tabs>
  );
}
