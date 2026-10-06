/**
 * The `/link?...` address a `/setup?next=` names, and nothing else - never an
 * arbitrary path. `search` is a location's query string (`?next=...`).
 */
export function safeLinkNext(search: string | undefined): string | null {
  const next = new URLSearchParams(search ?? "").get("next");
  return next && /^\/link(\?[^#]*)?$/.test(next) ? next : null;
}
