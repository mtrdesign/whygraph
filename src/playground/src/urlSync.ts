import { useExplorer, type View } from "./store";

// Bidirectional projection between the address bar and the zustand store, so a
// refresh, a bookmark, and the back button all work.
//
// Deliberately not a router. The store is already the single source of truth
// with one canonical mutation point per transition (`openNode`, `setView`,
// `setActiveSession`); a router would invert that to URL-as-truth and touch
// every call site to buy nested routing two views don't have. This module only
// observes and seeds — no component imports it, no component knows it exists.
//
// Exactly four fields are encoded. DetailPanel's tab, tree expansion, and graph
// positions stay component-local: they self-restore acceptably, and putting them
// in the URL would make every click a history entry.
//
//   /explorer                          view: explorer, nothing selected
//   /explorer?node=<qn>                + selectedQn
//   /explorer?node=<qn>&file=<path>    + selectedFilePath (drives tree reveal)
//   /chat                              view: chat, no session open
//   /chat/<id>                         + activeSessionId
//   anything else                      normalized to /explorer
//
// Values go through URLSearchParams in both directions, which is symmetric by
// construction — qualified names carry `.`, `:`, and `<>` from generics, and
// file paths carry `/`.

interface UrlState {
  view: View;
  selectedQn: string | null;
  selectedFilePath: string | null;
  activeSessionId: number | null;
}

/** The one URL that represents a given state. */
function buildUrl(state: UrlState): string {
  if (state.view === "chat") {
    return state.activeSessionId === null ? "/chat" : `/chat/${state.activeSessionId}`;
  }
  const params = new URLSearchParams();
  if (state.selectedQn) {
    params.set("node", state.selectedQn);
    // Only meaningful alongside a node — a bare ?file= selects nothing.
    if (state.selectedFilePath) params.set("file", state.selectedFilePath);
  }
  const query = params.toString();
  return query ? `/explorer?${query}` : "/explorer";
}

/**
 * Read the address bar into the four fields.
 *
 * Nothing here validates: a dead `/chat/999` or `?node=nope` is passed straight
 * through, and the thread's 404 state / the graph query's error state say so.
 * Guessing a replacement would be worse than showing the user what they asked
 * for and why it didn't resolve.
 */
function parseUrl(): UrlState {
  const path = window.location.pathname.replace(/\/+$/, "");
  const params = new URLSearchParams(window.location.search);

  if (path === "/chat" || path.startsWith("/chat/")) {
    const rest = path.slice("/chat/".length);
    // Only a plain integer opens a session; `/chat/` and `/chat/junk` are `/chat`.
    const id = path === "/chat" || !/^\d+$/.test(rest) ? null : Number(rest);
    return {
      view: "chat",
      selectedQn: null,
      selectedFilePath: null,
      activeSessionId: id,
    };
  }

  // `/explorer` and every unrecognized path land here; buildUrl then normalizes
  // the address bar, so `/` and `/typo` both settle on `/explorer`.
  return {
    view: "explorer",
    selectedQn: params.get("node") || null,
    selectedFilePath: params.get("file") || null,
    activeSessionId: null,
  };
}

function currentUrl(): string {
  return window.location.pathname + window.location.search;
}

/** Seed the store from the address bar, normalizing the bar in the same breath. */
function applyUrl(): void {
  const next = parseUrl();
  // Canonicalize *before* the store write: the subscriber's guard compares the
  // URL it would build against the address bar, so making them agree first is
  // what stops a popstate from pushing a duplicate entry. replaceState, not
  // push — landing on `/` is not a navigation the user made.
  const canonical = buildUrl(next);
  if (canonical !== currentUrl()) {
    window.history.replaceState(null, "", canonical);
  }
  useExplorer.setState(next);
}

/**
 * Wire the store to the address bar. Call once from module scope in `main.tsx`
 * — **not** from an effect, which StrictMode double-invokes.
 */
export function initUrlSync(): void {
  applyUrl();
  window.addEventListener("popstate", applyUrl);

  useExplorer.subscribe((state, previous) => {
    const url = buildUrl(state);
    // The loop guard. A popstate-driven write regenerates the URL already in
    // the bar, so this no-ops instead of pushing the entry we just came from.
    if (url === currentUrl()) return;

    // Gaining or losing the file hint for the same node isn't a distinct place
    // — a chat chip opens a node without a path, the tree opens it with one.
    const sameNode =
      state.view === "explorer" &&
      previous.view === "explorer" &&
      state.selectedQn === previous.selectedQn;
    if (sameNode) window.history.replaceState(null, "", url);
    else window.history.pushState(null, "", url);
  });
}
