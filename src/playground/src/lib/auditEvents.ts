// The audit log's words: one label and one group per event name the backend writes
// (`audit("...")` in `src/whygraph/portal`), so the table, the event filter and the
// docs agree. A new event needs an entry here; `auditEvents.test.ts` checks that every
// event in docs/deploy/production.md's security event table has one.

import type { AuditEventRow } from "../api";
import { formatUsd } from "./format";

export type AuditGroup = "Members" | "Projects" | "Budgets" | "Security" | "Organization";

/** The groups in the order the event select lists them. */
export const AUDIT_GROUPS: AuditGroup[] = ["Members", "Projects", "Budgets", "Security", "Organization"];

interface EventInfo {
  label: string;
  group: AuditGroup;
}

export const AUDIT_EVENTS: Record<string, EventInfo> = {
  // Members
  member_added: { label: "Member added", group: "Members" },
  member_add_refused: { label: "Adding a member refused", group: "Members" },
  member_role_changed: { label: "Member role changed", group: "Members" },
  member_removed: { label: "Member removed", group: "Members" },
  member_left: { label: "Member left", group: "Members" },
  member_joined_by_invite: { label: "Joined by invitation", group: "Members" },
  invitation_created: { label: "Invitation created", group: "Members" },
  invitation_revoked: { label: "Invitation revoked", group: "Members" },
  user_disabled: { label: "Account disabled", group: "Members" },
  user_enabled: { label: "Account enabled", group: "Members" },
  // Projects
  project_imported: { label: "Project imported", group: "Projects" },
  project_removed: { label: "Project removed", group: "Projects" },
  project_grant_added: { label: "Project access given", group: "Projects" },
  project_grant_changed: { label: "Project access changed", group: "Projects" },
  project_grant_removed: { label: "Project access removed", group: "Projects" },
  project_restricted_changed: { label: "Restricted setting changed", group: "Projects" },
  project_access_lost: { label: "GitHub access lost", group: "Projects" },
  project_access_restored: { label: "GitHub access restored", group: "Projects" },
  // Budgets
  budget_threshold_crossed: { label: "Budget threshold crossed", group: "Budgets" },
  budget_hard_stop_engaged: { label: "Budget hard stop engaged", group: "Budgets" },
  budget_set: { label: "Budget set", group: "Budgets" },
  budget_removed: { label: "Budget removed", group: "Budgets" },
  price_override_set: { label: "Price set", group: "Budgets" },
  price_override_removed: { label: "Price reverted", group: "Budgets" },
  // Security
  bootstrap_claimed: { label: "Instance set up", group: "Security" },
  login_success: { label: "Signed in", group: "Security" },
  login_failure: { label: "Sign-in failed", group: "Security" },
  logout: { label: "Signed out", group: "Security" },
  password_changed: { label: "Password changed", group: "Security" },
  reset_link_issued: { label: "Reset link issued", group: "Security" },
  reset_link_used: { label: "Reset link used", group: "Security" },
  admin_granted: { label: "Instance admin granted", group: "Security" },
  admin_revoked: { label: "Instance admin revoked", group: "Security" },
  github_signin: { label: "Signed in with GitHub", group: "Security" },
  github_signin_refused: { label: "GitHub sign-in refused", group: "Security" },
  github_token_revoke_failed: { label: "GitHub token not revoked", group: "Security" },
  github_login_released: { label: "GitHub username released", group: "Security" },
  github_app_authorized: { label: "GitHub App authorized", group: "Security" },
  github_account_mismatch: { label: "GitHub account mismatch", group: "Security" },
  connection_authorized: { label: "Connected portal allowed", group: "Security" },
  connection_token_issued: { label: "Connection token issued", group: "Security" },
  connection_token_refused: { label: "Connection token refused", group: "Security" },
  connection_revoked: { label: "Connection revoked", group: "Security" },
  webhook_rejected: { label: "Webhook rejected", group: "Security" },
  reader_request: { label: "Instance admin read", group: "Security" },
  key_tested: { label: "API key tested", group: "Security" },
  // Organization
  org_created: { label: "Organization created", group: "Organization" },
  org_renamed: { label: "Organization renamed", group: "Organization" },
  org_default_role_changed: { label: "Default project role changed", group: "Organization" },
  org_ownership_transferred: { label: "Ownership transferred", group: "Organization" },
  org_deleted: { label: "Organization deleted", group: "Organization" },
};

/** "Member added" for `member_added`; an unknown event reads as its name with spaces. */
export function auditLabel(event: string): string {
  const known = Object.prototype.hasOwnProperty.call(AUDIT_EVENTS, event) ? AUDIT_EVENTS[event] : undefined;
  if (known) return known.label;
  const spaced = event.replace(/_/g, " ").trim();
  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : event;
}

/** The events of one group, in the order they are declared. */
export function auditEventsIn(group: AuditGroup): { event: string; label: string }[] {
  return Object.entries(AUDIT_EVENTS)
    .filter(([, info]) => info.group === group)
    .map(([event, info]) => ({ event, label: info.label }));
}

const FIELD_LABELS: Record<string, string> = {
  monthly_usd: "Monthly budget",
  hard_stop: "Hard stop",
  budget_usd: "Budget",
  spent_usd: "Spent",
  new: "New account",
  ip: "Address",
};

const USD_FIELDS = new Set(["monthly_usd", "budget_usd", "spent_usd"]);

function fieldLabel(key: string): string {
  if (FIELD_LABELS[key]) return FIELD_LABELS[key];
  const spaced = key.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function fieldValue(key: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (USD_FIELDS.has(key)) {
    const n = Number(value);
    if (Number.isFinite(n)) return formatUsd(n);
  }
  if (typeof value === "string" || typeof value === "number") return String(value);
  return JSON.stringify(value);
}

/**
 * An audit row's `fields` (plus the organization on the instance admin's log) as
 * label / value pairs, for a details cell.
 */
export function auditDetails(row: AuditEventRow, withOrg = false): { label: string; value: string }[] {
  const pairs = Object.entries(row.fields ?? {}).map(([key, value]) => ({
    label: fieldLabel(key),
    value: fieldValue(key, value),
  }));
  return withOrg && row.org ? [{ label: "Organization", value: row.org }, ...pairs] : pairs;
}
