// What the update notice says and remembers (plan 43, section 4.9). The notice is
// computed by the portal; the only state kept here is a per-viewer dismissal of one
// version, in `localStorage` (a convenience: it can be absent).

/** Why the update check is on or off, for the switch's hint; empty when the viewer chose it. */
export function sourceLabel(source: string | null | undefined): string {
  if (source === "do_not_track") return "Off: DO_NOT_TRACK is set";
  if (source === "env") return "Set by WHYGRAPH_UPDATE_CHECK";
  return "";
}

/** The words for a failed check's code (never the code itself). */
export function errorLabel(error: string | null | undefined): string {
  if (error === "network") return "could not reach GitHub";
  if (error === "rate_limited") return "GitHub rate limit, will retry";
  return "unexpected answer from GitHub";
}

const key = (version: string) => `whygraph:update-banner:${version}`;

/** Whether the viewer dismissed the notice for `version`. */
export function isUpdateDismissed(version: string): boolean {
  try {
    return window.localStorage.getItem(key(version)) === "1";
  } catch {
    return false;
  }
}

/** Remember a dismissal (a no-op where storage is blocked). */
export function dismissUpdate(version: string): void {
  try {
    window.localStorage.setItem(key(version), "1");
  } catch {
    // Storage blocked: the banner simply comes back on the next load.
  }
}
