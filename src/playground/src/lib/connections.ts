import type { RevokedReason } from "../api";

/** What a revoked connection token's reason means to the person reading the list. */
const REASONS: Record<RevokedReason, string> = {
  user_revoked: "Revoked by you",
  admin_revoked: "Revoked by an admin",
  removed_locally: "Removed from that machine",
  member_removed: "You were removed from the organization",
  member_left: "You left the organization",
  user_disabled: "The account was disabled",
  project_deleted: "The project was deleted",
  org_deleted: "The organization was deleted",
  idle: "Unused for too long",
  project_access_removed: "Your access to the project was removed",
};

export function revokedLabel(reason: RevokedReason | string | null): string {
  if (reason && Object.prototype.hasOwnProperty.call(REASONS, reason)) return REASONS[reason as RevokedReason];
  return "Revoked";
}

/** The same reasons as a project admin reads them about someone else's token. */
const ADMIN_REASONS: Record<RevokedReason, string> = {
  user_revoked: "Revoked by its owner",
  admin_revoked: "Revoked by an admin",
  removed_locally: "Removed from that machine",
  member_removed: "The member was removed from the organization",
  member_left: "The member left the organization",
  user_disabled: "The account was disabled",
  project_deleted: "The project was deleted",
  org_deleted: "The organization was deleted",
  idle: "Unused for too long",
  project_access_removed: "The member's access to the project was removed",
};

export function revokedLabelForAdmin(reason: RevokedReason | string | null): string {
  if (reason && Object.prototype.hasOwnProperty.call(ADMIN_REASONS, reason)) return ADMIN_REASONS[reason as RevokedReason];
  return "Revoked";
}

// ---- the "Use with your agent" deep link (plan section 4.6) ---------------------

export const LOCAL_PORT_KEY = "whygraph.localPort";
export const DEFAULT_LOCAL_PORT = 8765;

/** A usable TCP port, or `null`. */
export function parsePort(raw: string): number | null {
  if (!/^[0-9]{1,5}$/.test(raw.trim())) return null;
  const n = Number(raw);
  return n >= 1 && n <= 65535 ? n : null;
}

/** The port the person last used, from `localStorage` (which may throw or be empty). */
export function readStoredPort(): string {
  try {
    const raw = window.localStorage.getItem(LOCAL_PORT_KEY);
    if (raw && parsePort(raw) !== null) return raw;
  } catch {
    // storage blocked: fall through to the default
  }
  return String(DEFAULT_LOCAL_PORT);
}

export function storePort(port: string): void {
  try {
    window.localStorage.setItem(LOCAL_PORT_KEY, port);
  } catch {
    // a convenience only
  }
}

/** `http://127.0.0.1:<port>/link?platform=<origin>&org=<org>&project=<slug>`. */
export function localLinkUrl(port: number, platformOrigin: string, org: string, project: string): string {
  const q = new URLSearchParams({ platform: platformOrigin, org, project });
  return `http://127.0.0.1:${port}/link?${q.toString()}`;
}
