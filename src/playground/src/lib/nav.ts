import { useCallback } from "react";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useSlug } from "./project";

// Navigation is the router's job: which symbol is open (`?node=&file=`) and which
// chat session is open (`/chat/<id>`) are URL state, not store state, so they
// cannot outlive a project switch and every deep link is shareable.

export interface ExplorerSearch {
  node?: string;
  file?: string;
}

/** The Explorer's `?node=&file=` (empty on any other route). */
export function useExplorerSearch(): ExplorerSearch {
  // `strict: false` would hand back the raw, unvalidated search; asking the
  // Explorer route for its own (validated) search returns undefined elsewhere.
  return (useSearch({ from: "/p/$slug/explorer", shouldThrow: false }) ?? {}) as ExplorerSearch;
}

/**
 * The one way to open a symbol - Cmd-K, graph-node clicks, relationship rows and
 * chat chips all call it. Always lands on the current project's Explorer.
 * Re-opening the same node (only the file hint changed) replaces the history
 * entry: gaining or losing the hint is not a distinct place.
 */
export function useOpenNode() {
  const navigate = useNavigate();
  const slug = useSlug();
  const current = useExplorerSearch();
  return useCallback(
    (qualifiedName: string, filePath?: string) =>
      navigate({
        to: "/p/$slug/explorer",
        params: { slug },
        search: { node: qualifiedName, file: filePath },
        replace: current.node === qualifiedName,
      }),
    [navigate, slug, current.node],
  );
}

/** The open chat session id, or `null` (`/chat` and `/chat/<junk>` open none). */
export function useActiveSessionId(): number | null {
  const { id } = useParams({ strict: false }) as { id?: string };
  return id !== undefined && /^\d+$/.test(id) ? Number(id) : null;
}

export function useSetActiveSession() {
  const navigate = useNavigate();
  const slug = useSlug();
  return useCallback(
    (id: number | null) =>
      navigate({
        to: "/p/$slug/chat/{-$id}",
        params: { slug, id: id === null ? undefined : String(id) },
      }),
    [navigate, slug],
  );
}
