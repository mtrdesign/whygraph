// The last project the user had open, so the old root-level `/explorer` and
// `/chat` links (bookmarks, chat deep links) can land somewhere sensible. It is
// only a hint - callers validate it against `GET /api/projects` - and storage can
// be blocked or throw, so every access is guarded.

export const LAST_PROJECT_KEY = "lastProject";

export function getLastProject(): string | null {
  try {
    return window.localStorage.getItem(LAST_PROJECT_KEY);
  } catch {
    return null;
  }
}

export function setLastProject(slug: string): void {
  try {
    window.localStorage.setItem(LAST_PROJECT_KEY, slug);
  } catch {
    // Private window / blocked storage: the hint is a convenience, not state.
  }
}
