import { useDefaultLayout } from "react-resizable-panels";
import { Tree } from "../components/Tree";
import { GraphCanvas } from "../components/GraphCanvas";
import { Overview } from "../components/Overview";
import { DetailPanel } from "../components/DetailPanel";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "../components/ui/resizable";
import { PhoneExplorer } from "../components/PhoneExplorer";
import { useExplorerSearch } from "../lib/nav";
import { useIsPhone } from "../lib/useIsPhone";
import { useSlug } from "../lib/project";
import { useQuery } from "@tanstack/react-query";
import { portalApi, projectKey } from "../api";

const PANEL_IDS = ["tree", "canvas", "detail"];

// The Explorer's three resizable panes (tree | graph | detail). What is selected
// is the route's `?node=&file=`, so a reload, a bookmark and the back button all
// restore it; the pane widths are remembered per browser.
export function ExplorerPage() {
  const { node } = useExplorerSearch();
  const phone = useIsPhone();
  const slug = useSlug();
  const project = useQuery({ queryKey: projectKey(slug, "project"), queryFn: () => portalApi.project(slug) });
  const layout = useDefaultLayout({ id: "whygraph-explorer", panelIds: PANEL_IDS });
  const heading = <h1 className="sr-only">Explorer - {project.data?.name ?? slug}</h1>;
  if (phone)
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        {heading}
        <PhoneExplorer />
      </div>
    );
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {heading}
    <ResizablePanelGroup
      orientation="horizontal"
      className="min-h-0 flex-1"
      defaultLayout={layout.defaultLayout}
      onLayoutChanged={layout.onLayoutChanged}
    >
      <ResizablePanel id="tree" defaultSize="20%" minSize="14%" maxSize="40%" className="bg-sidebar">
        <Tree />
      </ResizablePanel>
      <ResizableHandle />
      <ResizablePanel id="canvas" defaultSize="50%" minSize="25%" className="relative bg-background">
        {node ? <GraphCanvas /> : <Overview />}
      </ResizablePanel>
      <ResizableHandle />
      <ResizablePanel id="detail" defaultSize="30%" minSize="20%" maxSize="50%" className="bg-sidebar">
        <DetailPanel />
      </ResizablePanel>
    </ResizablePanelGroup>
    </div>
  );
}
