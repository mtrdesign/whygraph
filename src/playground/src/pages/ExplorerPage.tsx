import { Tree } from "../components/Tree";
import { GraphCanvas } from "../components/GraphCanvas";
import { Overview } from "../components/Overview";
import { DetailPanel } from "../components/DetailPanel";
import { useExplorerSearch } from "../lib/nav";

// The Explorer's three panes. What is selected is the route's `?node=&file=`, so
// a reload, a bookmark and the back button all restore it.
export function ExplorerPage() {
  const { node } = useExplorerSearch();
  return (
    <div className="flex min-h-0 flex-1">
      <aside className="w-72 shrink-0 border-r border-border bg-panel">
        <Tree />
      </aside>
      <main className="relative min-w-0 flex-1 bg-bg">
        {node ? <GraphCanvas /> : <Overview />}
      </main>
      <aside className="w-96 shrink-0 border-l border-border bg-panel">
        <DetailPanel />
      </aside>
    </div>
  );
}
