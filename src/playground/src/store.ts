import { create } from "zustand";

// The single source of truth for "what symbol is open". Cmd-K, graph-node
// clicks, and relationship-list rows all call `openNode()` — the one canonical
// navigation entry point (§7.2). Everything else derives from `selectedQn`.
interface ExplorerState {
  selectedQn: string | null;
  // File path of the selected symbol when the caller knows it — lets the tree
  // auto-reveal the containing directory path without an extra lookup.
  selectedFilePath: string | null;
  paletteOpen: boolean;
  openNode: (qualifiedName: string, filePath?: string) => void;
  setPaletteOpen: (open: boolean) => void;
}

export const useExplorer = create<ExplorerState>((set) => ({
  selectedQn: null,
  selectedFilePath: null,
  paletteOpen: false,
  openNode: (qualifiedName, filePath) =>
    set({
      selectedQn: qualifiedName,
      selectedFilePath: filePath ?? null,
      paletteOpen: false,
    }),
  setPaletteOpen: (open) => set({ paletteOpen: open }),
}));
