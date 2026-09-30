import type { Detected } from "../api";

// `detected` (the 1.x state a repo carries) is returned once, by the add call, and
// there is no GET for it. The wizard navigates away from the add step, so it is
// kept in sessionStorage for the Initialize step, which survives a reload and
// lets a half-finished wizard resume. Without it (another browser, a new tab)
// the Initialize step still works from the dry-run preview alone.

const key = (slug: string) => `whygraph:detected:${slug}`;

export function saveDetected(slug: string, detected: Detected): void {
  try {
    sessionStorage.setItem(key(slug), JSON.stringify(detected));
  } catch {
    // Not persisted; the panel is simply absent on resume.
  }
}

export function loadDetected(slug: string): Detected | null {
  try {
    const raw = sessionStorage.getItem(key(slug));
    return raw ? (JSON.parse(raw) as Detected) : null;
  } catch {
    return null;
  }
}

export function clearDetected(slug: string): void {
  try {
    sessionStorage.removeItem(key(slug));
  } catch {
    // Nothing to clear.
  }
}
