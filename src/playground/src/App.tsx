import { useEffect } from "react";
import { clsx } from "clsx";
import { Tree } from "./components/Tree";
import { GraphCanvas } from "./components/GraphCanvas";
import { Overview } from "./components/Overview";
import { DetailPanel } from "./components/DetailPanel";
import { CommandPalette } from "./components/CommandPalette";
import { ChatView } from "./components/chat/ChatView";
import { useExplorer, type View } from "./store";

const VIEWS: { key: View; label: string }[] = [
  { key: "explorer", label: "Explorer" },
  { key: "chat", label: "Chat" },
];

// A segmented control rather than routes: no routing library is in play, and the
// house tab idiom (DetailPanel) is already this shape.
function ViewSwitch() {
  const view = useExplorer((s) => s.view);
  const setView = useExplorer((s) => s.setView);
  return (
    <div className="flex rounded-md border border-border bg-panel2 p-0.5">
      {VIEWS.map((v) => (
        <button
          key={v.key}
          onClick={() => setView(v.key)}
          className={clsx(
            "rounded px-3 py-1 text-xs font-medium transition-colors",
            view === v.key ? "bg-accent text-white" : "text-muted hover:text-fg",
          )}
        >
          {v.label}
        </button>
      ))}
    </div>
  );
}

export default function App() {
  const setPaletteOpen = useExplorer((s) => s.setPaletteOpen);
  const selectedQn = useExplorer((s) => s.selectedQn);
  const view = useExplorer((s) => s.view);

  // Global ⌘K / Ctrl-K opens the command palette. It stays global across views:
  // opening a result calls `openNode()`, which switches to the Explorer, so a
  // search from the Chat view always lands somewhere that can show the symbol.
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
          <span className="text-accent2">◆</span> WhyGraph
        </div>
        <ViewSwitch />
        <button
          onClick={() => setPaletteOpen(true)}
          className="flex items-center gap-2 rounded-md border border-border bg-panel2 px-3 py-1 text-xs text-muted hover:text-fg"
        >
          Search
          <kbd className="rounded border border-border px-1.5 py-0.5 text-[10px]">⌘K</kbd>
        </button>
      </header>

      {/* Each view's state lives in the store (selectedQn, activeSessionId), so
          switching away and back is lossless even though the tree unmounts. */}
      {view === "explorer" ? (
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
      ) : (
        <ChatView />
      )}

      <CommandPalette />
    </div>
  );
}
