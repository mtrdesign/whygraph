import { useEffect } from "react";
import { Tree } from "./components/Tree";
import { GraphCanvas } from "./components/GraphCanvas";
import { Overview } from "./components/Overview";
import { DetailPanel } from "./components/DetailPanel";
import { CommandPalette } from "./components/CommandPalette";
import { useExplorer } from "./store";

export default function App() {
  const setPaletteOpen = useExplorer((s) => s.setPaletteOpen);
  const selectedQn = useExplorer((s) => s.selectedQn);

  // Global ⌘K / Ctrl-K opens the command palette.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setPaletteOpen]);

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center justify-between border-b border-border bg-panel px-4 py-2">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <span className="text-accent2">◆</span> WhyGraph Explorer
        </div>
        <button
          onClick={() => setPaletteOpen(true)}
          className="flex items-center gap-2 rounded-md border border-border bg-panel2 px-3 py-1 text-xs text-muted hover:text-fg"
        >
          Search
          <kbd className="rounded border border-border px-1.5 py-0.5 text-[10px]">⌘K</kbd>
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="w-72 shrink-0 border-r border-border bg-panel">
          <Tree />
        </aside>
        <main className="relative min-w-0 flex-1 bg-bg">
          {selectedQn ? <GraphCanvas /> : <Overview />}
        </main>
        <aside className="w-96 shrink-0 border-l border-border bg-panel">
          <DetailPanel />
        </aside>
      </div>

      <CommandPalette />
    </div>
  );
}
