import { create } from "zustand";

// The single source of truth for "what symbol is open". Cmd-K, graph-node
// clicks, and relationship-list rows all call `openNode()` — the one canonical
// navigation entry point (§7.2). Everything else derives from `selectedQn`.
//
// Top-level view switching is a store field rather than a router: no routing
// library is in play, and both views are single screens whose state should
// survive switching away and back. `openNode()` also switches to the Explorer,
// so a Cmd-K search or a chat deep-link from the Chat view lands somewhere that
// can actually show the result.
export type View = "explorer" | "chat";

interface ExplorerState {
  view: View;
  selectedQn: string | null;
  // File path of the selected symbol when the caller knows it — lets the tree
  // auto-reveal the containing directory path without an extra lookup.
  selectedFilePath: string | null;
  paletteOpen: boolean;
  // Which chat session the Chat view has open; null shows the empty state.
  activeSessionId: number | null;
  setView: (view: View) => void;
  openNode: (qualifiedName: string, filePath?: string) => void;
  setPaletteOpen: (open: boolean) => void;
  setActiveSession: (id: number | null) => void;
}

export const useExplorer = create<ExplorerState>((set) => ({
  view: "explorer",
  selectedQn: null,
  selectedFilePath: null,
  paletteOpen: false,
  activeSessionId: null,
  setView: (view) => set({ view }),
  openNode: (qualifiedName, filePath) =>
    set({
      view: "explorer",
      selectedQn: qualifiedName,
      selectedFilePath: filePath ?? null,
      paletteOpen: false,
    }),
  setPaletteOpen: (open) => set({ paletteOpen: open }),
  setActiveSession: (id) => set({ activeSessionId: id }),
}));
