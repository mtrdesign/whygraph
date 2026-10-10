import { create } from "zustand";

// UI state only. Which project, page, symbol (`?node=&file=`), chat session or
// scan run is open belongs to the router (see router.tsx and lib/nav.ts), so it
// can never survive a project switch by accident.
interface UiState {
  paletteOpen: boolean;
  // The sidebar is a sheet below 768 px; this is its open flag.
  navOpen: boolean;
  // The chat session whose reply is streaming right now, or `null`. Set and cleared
  // by `MessageThread` (the only component that knows); the sidebar's Chats section
  // shows a spinner on that row.
  streamingSessionId: number | null;
  setPaletteOpen: (open: boolean) => void;
  setNavOpen: (open: boolean) => void;
  setStreamingSessionId: (id: number | null) => void;
}

export const useUi = create<UiState>((set) => ({
  paletteOpen: false,
  navOpen: false,
  streamingSessionId: null,
  setPaletteOpen: (open) => set({ paletteOpen: open }),
  setNavOpen: (open) => set({ navOpen: open }),
  setStreamingSessionId: (id) => set({ streamingSessionId: id }),
}));
