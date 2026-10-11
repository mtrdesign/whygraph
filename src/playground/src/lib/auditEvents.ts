// The audit log's words: one label and one group per event name the backend writes
// (`audit("...")` in `src/whygraph/portal`), so the table, the event filter and the
// docs agree. A new event needs an entry here; `auditEvents.test.ts` checks that every
// event in docs/deploy/production.md's security event table has one.

import type { AuditEventRow } from "../api";
import { formatMonth, formatUsd, formatUsdPrecise } from "./format";
import { providerLabel, sourceLabel } from "./labels";

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
  new_user: "New account",
  ip: "Address",
  github_login: "GitHub login",
  github_id: "GitHub account id",
  github_event: "GitHub event",
  email: "Email",
  invitation: "Invitation",
  token_uid: "Connection",
  client_name: "Portal name",
  repo_id: "Repository id",
  full_name: "Repository",
  previous: "Before",
  grants: "Project grants",
  projects: "Projects",
  tokens_revoked: "Connection tokens revoked",
  checkout_deleted: "Checkout deleted",
  arrival: "How",
  input_per_mtok: "Input price",
  output_per_mtok: "Output price",
  cache_read_per_mtok: "Cache read price",
  cache_write_per_mtok: "Cache write price",
};

const USD_FIELDS = new Set(["monthly_usd", "budget_usd", "spent_usd"]);
const RATE_FIELDS = new Set(["input_per_mtok", "output_per_mtok", "cache_read_per_mtok", "cache_write_per_mtok"]);
const ROLE_FIELDS = new Set(["role", "previous"]);

/** Every `reason` word the backend audits, in words (sign-in, members, connections, access, webhooks). */
const REASONS: Record<string, string> = {
  // GitHub sign-in
  oauth_state: "The sign-in request expired or was not this browser's",
  unavailable: "GitHub was unavailable",
  "2fa_required": "Two-factor authentication is required",
  disabled: "The account is disabled",
  password_change: "A password change is required",
  // Adding a member
  bad_login: "Not a valid GitHub username",
  owner_required: "Only an owner can add an owner",
  throttled: "Too many attempts",
  no_such_github_user: "No such GitHub user",
  github_rate_limited: "GitHub's rate limit was reached",
  github_unavailable: "GitHub was unavailable",
  no_such_project: "A project that is not in the organization",
  user_disabled: "The account is disabled",
  already_member: "Already a member",
  already_invited: "Already invited",
  // Connected portals
  unknown_code: "Unknown, used or expired code",
  pkce: "The verifier did not match",
  redirect_uri: "The return address did not match",
  project_deleted: "The project was deleted",
  member_removed: "No longer a member",
  project_access_removed: "No longer has access to the project",
  user_revoked: "Revoked by its owner",
  admin_revoked: "Revoked by an admin",
  removed_locally: "Removed on the connected portal",
  // GitHub access
  no_access: "The GitHub App has no access to the repository",
  git_access_denied: "GitHub refused the fetch",
  repo_deleted: "The repository was deleted",
  tracked_whygraph_state: "The default branch tracks WhyGraph's own files",
  // Webhooks
  missing_signature: "No signature",
  bad_signature: "Wrong signature",
};

const RESULTS: Record<string, string> = {
  ok: "Works",
  rejected: "Rejected by the provider",
  rate_limited: "Rate limited",
  unreachable: "Unreachable",
  no_repo_access: "No access to the repository",
  unexpected: "Unexpected answer",
};

const SCOPES: Record<string, string> = {
  org: "Organization",
  project: "Project",
  member: "Member",
  member_default: "Every member",
};

const ARRIVALS: Record<string, string> = { install: "Installed the app", authorize: "Authorized the app" };

const ROLES: Record<string, string> = {
  owner: "Owner",
  admin: "Admin",
  member: "Member",
  contributor: "Contributor",
  viewer: "Viewer",
  none: "None",
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** One details pair: `title` holds the full value when `value` is shortened; `mono` sets it as an id. */
export interface AuditDetail {
  label: string;
  value: string;
  title?: string;
  mono?: boolean;
}

function fieldLabel(key: string): string {
  if (FIELD_LABELS[key]) return FIELD_LABELS[key];
  const spaced = key.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** `76b37542-8a8a-...` as its first block, with the whole id kept for the title. */
export function shortId(id: string): { value: string; title: string; mono: true } {
  return { value: id.slice(0, 8), title: id, mono: true };
}

/** A per-million-token rate as money: `3.000000` -> `$3.00 per million tokens`. */
function rateText(n: number): string {
  return `${formatUsdPrecise(n, 2)} per million tokens`;
}

function fieldValue(key: string, value: unknown): Omit<AuditDetail, "label"> | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "boolean") return { value: value ? "Yes" : "No" };
  if (USD_FIELDS.has(key) || RATE_FIELDS.has(key)) {
    const n = Number(value);
    if (Number.isFinite(n)) return { value: RATE_FIELDS.has(key) ? rateText(n) : formatUsd(n) };
  }
  if (typeof value === "number") return { value: key === "threshold" ? `${value}%` : String(value) };
  if (typeof value !== "string") return { value: JSON.stringify(value) };
  if (UUID.test(value)) return shortId(value);
  if (key === "month" && /^\d{4}-\d{2}$/.test(value)) return { value: formatMonth(value), title: value };
  const known = (table: Record<string, string>) => (Object.prototype.hasOwnProperty.call(table, value) ? table[value] : undefined);
  let worded: string | undefined;
  if (key === "reason") worded = known(REASONS);
  else if (key === "result") worded = known(RESULTS);
  else if (key === "scope") worded = known(SCOPES);
  else if (key === "provider") worded = providerLabel(value);
  else if (key === "source") worded = sourceLabel(value);
  else if (key === "arrival") worded = known(ARRIVALS);
  else if (ROLE_FIELDS.has(key)) worded = known(ROLES);
  if (worded) return { value: worded, title: value };
  // A quoted name (`'Ada's laptop'`, as the backend logs it): the name itself.
  if (key === "client_name") return { value: value.replace(/^(['"])(.*)\1$/, "$2") };
  if (key === "github_login") return { value: `@${value}` };
  if (key === "model" || key === "path" || key === "delivery") return { value, mono: true };
  if (key === "reason") {
    // An unknown reason word: its words, never the raw key.
    const spaced = value.replace(/_/g, " ");
    return { value: spaced.charAt(0).toUpperCase() + spaced.slice(1), title: value };
  }
  return { value };
}

/**
 * An audit row's `fields` (plus the organization on the instance admin's log) as
 * label / value pairs, for a details cell: codes in words, ids shortened (whole in
 * `title`), money as money, and an empty field left out rather than a bare label.
 */
export function auditDetails(row: AuditEventRow, withOrg = false): AuditDetail[] {
  const pairs: AuditDetail[] = [];
  for (const [key, value] of Object.entries(row.fields ?? {})) {
    const shown = fieldValue(key, value);
    if (shown) pairs.push({ label: fieldLabel(key), ...shown });
  }
  return withOrg && row.org ? [{ label: "Organization", value: row.org }, ...pairs] : pairs;
}

/** The Target cell: the server's label, else the raw target (an id shortened), else nothing. */
export function auditTarget(row: AuditEventRow): Omit<AuditDetail, "label"> | null {
  if (row.target_label) return { value: row.target_label };
  if (!row.target) return null;
  return UUID.test(row.target) ? shortId(row.target) : { value: row.target };
}
