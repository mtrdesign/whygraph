import type { AccessLostReason, ProjectDetails, ProjectOverview, ProjectSummary } from "../api";
import { providerLabel } from "./labels";
import { can } from "./permissions";
import { linkNotice } from "./platformLink";
import { plural } from "./plural";

// One precedence for "what is the state of this project": the Overview's health
// panel lists every item, the Projects card and the switcher show the first
// item's pill (M2f-3 plan section 4.10, section 0.3 #16 / #38), so a card and its
// Overview never disagree.

export type StatusTone = "ready" | "busy" | "warn" | "error" | "idle";

/** A pill's key, in the vocabulary of plan section 0.3 #38 (plus `unscanned`). */
export type StatusKey =
  | "link_revoked"
  | "no_access"
  | "unsupported"
  | "importing"
  | "import_failed"
  | "folder_missing"
  | "needs_setup"
  | "scanning"
  | "stopped"
  | "failed"
  | "behind"
  | "unscanned"
  | "ready";

export interface ProjectStatus {
  key: StatusKey;
  label: string;
  tone: StatusTone;
  /** Shown on hover and read after the label ("Monthly budget reached", "3 commits behind"). */
  tooltip?: string;
}

/** Worst first: the order the Projects list's Status sort uses. */
export const STATUS_ORDER: readonly StatusKey[] = [
  "link_revoked",
  "no_access",
  "unsupported",
  "import_failed",
  "folder_missing",
  "failed",
  "stopped",
  "behind",
  "needs_setup",
  "importing",
  "scanning",
  "unscanned",
  "ready",
];

export type HealthTone = "ok" | "info" | "warn" | "error";

export type HealthActionKind =
  | "reconnect"
  | "remove"
  | "manage"
  | "github"
  | "follow_run"
  | "retry_import"
  | "open_log"
  | "finish_setup"
  | "usage"
  | "add_key"
  | "retry_scan"
  | "rescan"
  | "describe";

export interface HealthAction {
  kind: HealthActionKind;
  label: string;
  /** The run a `follow_run` / `open_log` action opens (the scans page without one). */
  runId?: number;
}

export type HealthId =
  | "link"
  | "access"
  | "unsupported"
  | "importing"
  | "folder"
  | "unsafe_path"
  | "setup"
  | "budget"
  | "missing_key"
  | "failed"
  | "behind"
  | "waiting"
  | "unscanned"
  | "scanning";

export interface HealthItem {
  id: HealthId;
  tone: HealthTone;
  title: string;
  detail?: string;
  /** What fixes it, best first; a caller without the right sees none (the detail says whom to ask). */
  actions: HealthAction[];
}

export interface ProjectHealth {
  status: ProjectStatus;
  /** Empty when nothing is wrong ("Healthy"). */
  items: HealthItem[];
}

export interface HealthOptions {
  /** The Overview route's `last_failure`. */
  lastFailure?: ProjectOverview["last_failure"];
  /** Commits waiting for a description (the scan estimate's `commits`), when known. */
  waiting?: number | null;
  /** The project DB is behind a symbolic link (`409 unsafe_path`). */
  unsafePath?: boolean;
}

/** Why GitHub no longer lets WhyGraph read a production project, and the fix. */
const ACCESS: Record<AccessLostReason, { title: string; fix?: string }> = {
  no_access: { title: "The WhyGraph app can no longer read this repository", fix: "Reinstall the GitHub App" },
  git_access_denied: { title: "GitHub refused git access to this repository", fix: "Check repository access" },
  repo_deleted: { title: "The repository was deleted on GitHub" },
  tracked_whygraph_state: { title: "The default branch tracks .whygraph/ or .codegraph/" },
};

const ACCESS_DETAIL: Record<AccessLostReason, string> = {
  no_access: "What was scanned stays readable; scans are refused until the app can read it again.",
  git_access_denied: "What was scanned stays readable; scans are refused until GitHub allows git access again.",
  repo_deleted: "What was scanned stays readable. Remove the project if the repository is gone for good.",
  tracked_whygraph_state: "Remove .whygraph/ and .codegraph/ from the repository to scan again.",
};

function budgetDetail(scope: string | null | undefined): string {
  const what = "no new descriptions, rationale cards or Chat until it resets next month";
  if (scope === "member") return `Your monthly budget is spent and its hard stop is on: ${what}.`;
  if (scope === "org") return `The organization's monthly budget is spent and its hard stop is on: ${what}.`;
  return `This project's monthly budget is spent and its hard stop is on: ${what}.`;
}

/**
 * The project's health, in precedence order (plan section 4.10): a platform link
 * problem; GitHub access lost; an unsupported source; an import (running or
 * failed, which ranks above "folder missing": an import has no folder yet); the
 * folder missing / not a repository / a symbolic link in the way; setup not
 * finished; a hard-stopped budget; a missing key; the last scan failed; behind
 * the checkout; commits waiting for a description. `status` is the pill of the
 * first item that has one, else Scanning / Not scanned yet / Ready.
 */
export function projectHealth(
  p: ProjectSummary | ProjectDetails,
  { lastFailure = null, waiting = null, unsafePath = false }: HealthOptions = {},
): ProjectHealth {
  const items: HealthItem[] = [];
  let status: ProjectStatus | null = null;
  const pill = (s: ProjectStatus) => {
    status ??= s;
  };
  const linked = p.source === "platform";
  const scanning = !!p.running_scan;
  const mayScan = can(p, "project.scan");

  // 1. A platform link that no longer works (removed / revoked), or warns.
  if (linked && p.link && p.link.status !== "ok") {
    const n = linkNotice(p.link);
    const actions: HealthAction[] = [];
    if (n.actions.includes("reconnect")) actions.push({ kind: "reconnect", label: "Reconnect" });
    if (n.actions.includes("remove")) actions.push({ kind: "remove", label: "Remove from this machine" });
    if (n.actions.includes("manage")) actions.push({ kind: "manage", label: "Manage on platform" });
    items.push({ id: "link", tone: n.tone === "error" ? "error" : "warn", title: n.title, detail: n.detail, actions });
    if (p.link.status === "revoked" || p.link.status === "removed") {
      pill({ key: "link_revoked", label: "Link revoked", tone: "error", tooltip: n.title });
    }
  }

  // 2. GitHub access lost (production), by reason.
  if (p.access_lost) {
    const reason = p.access_lost_reason;
    const a = reason ? ACCESS[reason] : undefined;
    items.push({
      id: "access",
      tone: "error",
      title: a?.title ?? "GitHub no longer lets WhyGraph read this repository",
      detail: reason ? ACCESS_DETAIL[reason] : "What was scanned stays readable; scans are refused.",
      actions: a?.fix ? [{ kind: "github", label: a.fix }] : reason === "repo_deleted" ? [{ kind: "remove", label: "Remove the project" }] : [],
    });
    pill({ key: "no_access", label: "No access", tone: "error", tooltip: a?.title });
  }

  // 3. A source this mode no longer scans.
  if (p.source_supported === false) {
    items.push({
      id: "unsupported",
      tone: "error",
      title: "This source is no longer supported in local mode",
      detail: "What was scanned stays readable, but it cannot be scanned again. Remove the project.",
      actions: [{ kind: "remove", label: "Remove the project" }],
    });
    pill({ key: "unsupported", label: "Not supported", tone: "error" });
  }

  // 4. An import ranks above "folder missing": an importing project has no folder yet.
  if (p.importing) {
    const repo = p.github_full_name ?? p.name;
    if (scanning) {
      items.push({
        id: "importing",
        tone: "info",
        title: `Importing ${repo}`,
        detail: "WhyGraph is copying the repository and running its first scan. The Explorer and Chat open when it is done.",
        actions: [{ kind: "follow_run", label: "Follow the import", runId: p.running_scan!.id }],
      });
      pill({ key: "importing", label: "Importing", tone: "busy" });
    } else {
      const actions: HealthAction[] = [];
      if (mayScan) actions.push({ kind: "retry_import", label: "Retry import" });
      actions.push({ kind: "open_log", label: "Open log", runId: lastFailure?.run_id });
      items.push({
        id: "importing",
        tone: "error",
        title: "Import failed",
        detail: lastFailure?.message ?? "The import's last run says what went wrong.",
        actions,
      });
      pill({ key: "import_failed", label: "Import failed", tone: "error" });
    }
  } else if (p.root_status !== "ok") {
    // 5. The folder: missing or no longer a repository (the fix renders in the panel).
    items.push({
      id: "folder",
      tone: "error",
      title:
        p.root_status === "not_git" ? "This folder is no longer a git repository" : "The project folder is not available",
      actions: [],
    });
    pill({ key: "folder_missing", label: "Folder missing", tone: "error" });
  } else if (!p.initialized) {
    // 6. Added but never set up.
    const canSetup = can(p, "project.setup");
    items.push({
      id: "setup",
      tone: "warn",
      title: "Setup not finished",
      detail: canSetup
        ? "Connect your agents and run the first scan to fill the Explorer and Chat."
        : "A project admin has to finish setting it up before the Explorer and Chat have anything to show.",
      actions: canSetup ? [{ kind: "finish_setup", label: "Finish setup" }] : [],
    });
    pill({ key: "needs_setup", label: "Needs setup", tone: "idle" });
  }
  if (unsafePath) {
    items.push({ id: "unsafe_path", tone: "error", title: "A symbolic link is in the way", actions: [] });
  }

  const usable = p.initialized && p.root_status === "ok" && !p.importing;
  if (usable && scanning) pill({ key: "scanning", label: "Scanning", tone: "busy" });

  // 7. A hard-stopped budget: the role block wins (the caller cannot spend anyway).
  const usageStop = !!p.usage?.budget?.hard_stop && (p.usage.pct ?? 0) >= 100;
  if (!linked && (p.llm_block === "budget_exceeded" || usageStop)) {
    const scope = p.llm_block === "budget_exceeded" ? (p.llm_block_scope ?? "project") : "project";
    items.push({
      id: "budget",
      tone: "warn",
      title: "Monthly budget reached",
      detail: budgetDetail(scope),
      actions: can(p, "project.usage") ? [{ kind: "usage", label: "See usage and budgets" }] : [],
    });
    // A member's own budget is theirs, not the project's state.
    if (scope !== "member") pill({ key: "stopped", label: "Stopped", tone: "warn", tooltip: "Monthly budget reached" });
  }

  // 8. No key for the describe / rationale / chat model.
  const missingKey = "missing_key" in p ? p.missing_key : null;
  if (!linked && usable && missingKey) {
    const provider = providerLabel(missingKey);
    const canConfigure = can(p, "project.configure");
    items.push({
      id: "missing_key",
      tone: "warn",
      title: `No ${provider} key`,
      detail: `Commit descriptions, rationale cards and Chat need a ${provider} API key.${
        canConfigure ? "" : " Ask a project admin to add one."
      }`,
      actions: canConfigure ? [{ kind: "add_key", label: "Add a key" }] : [],
    });
  }

  // 9. The last scan failed (or was interrupted).
  const failedStatus = p.last_scan_status === "failed" || p.last_scan_status === "interrupted";
  if (usable && !scanning && (failedStatus || lastFailure)) {
    const actions: HealthAction[] = [];
    if (mayScan) actions.push({ kind: "retry_scan", label: "Retry" });
    actions.push({ kind: "open_log", label: "Open log", runId: lastFailure?.run_id });
    items.push({
      id: "failed",
      tone: "error",
      title: p.last_scan_status === "interrupted" ? "The last scan was interrupted" : "The last scan failed",
      detail: lastFailure?.message,
      actions,
    });
    pill({ key: "failed", label: "Scan failed", tone: "error" });
  }

  // 10. Behind the checkout.
  if (usable && p.stale) {
    const n = p.stale.commits_behind;
    const title = n === null ? "The repository history changed since the last scan" : `${plural(n, "commit")} behind`;
    items.push({
      id: "behind",
      tone: "warn",
      title,
      detail: linked
        ? "The checkout is ahead of the code index on this machine, so your agent places the newest work less precisely."
        : "The checkout is ahead of what WhyGraph last scanned, so the Explorer and Chat miss the newest work.",
      actions: mayScan ? [{ kind: "rescan", label: "Rescan" }] : [],
    });
    if (!scanning) pill({ key: "behind", label: "Behind", tone: "warn", tooltip: title });
  }

  // 11. Commits waiting for a description.
  if (usable && !linked && waiting && waiting > 0) {
    const canDescribe = can(p, "project.scan_full");
    items.push({
      id: "waiting",
      tone: "info",
      title: `${plural(waiting, "commit")} ${waiting === 1 ? "has" : "have"} no description yet`,
      detail: canDescribe
        ? "Describe them now, or let them fill in on demand as you browse."
        : "They fill in on demand as you browse. Ask a project admin to describe them all at once.",
      actions: canDescribe ? [{ kind: "describe", label: "Describe" }] : [],
    });
  }

  if (usable && !scanning && !p.last_scan_at) {
    items.push({
      id: "unscanned",
      tone: "info",
      title: "Not scanned yet",
      detail: "The first scan reads the git history and builds the code index; it calls no LLM.",
      actions: mayScan ? [{ kind: "rescan", label: "Scan now" }] : [],
    });
    pill({ key: "unscanned", label: "Not scanned yet", tone: "idle" });
  }
  if (usable && scanning) {
    items.push({
      id: "scanning",
      tone: "info",
      title: "A scan is running",
      actions: [{ kind: "follow_run", label: "Follow the scan", runId: p.running_scan!.id }],
    });
  }

  return { status: status ?? { key: "ready", label: "Ready", tone: "ready" }, items };
}

/** "Scanned 3 h ago" is hidden while it would mislead: scanning, importing, or no folder. */
export function showScannedAgo(p: ProjectSummary): boolean {
  return !!p.last_scan_at && !p.running_scan && !p.importing && p.root_status === "ok";
}
