import type { ProjectDetails, ProjectSummary } from "../api";
import { providerLabel } from "./labels";
import { can } from "./permissions";

/** Whether one kind of scan may be offered, and why not when it may not. */
export type Avail = { allowed: boolean; reason?: string };

export interface ScanAvailability {
  /** Quick rescan: git history and the code index, no LLM (`project.scan`). */
  quick: Avail;
  /** Full rescan: also describes new commits (`project.scan_full`). */
  full: Avail;
  /** An import whose clone did not finish: the quick scan is offered as **Retry import**. */
  retryImport: boolean;
}

const OK: Avail = { allowed: true };
const no = (reason: string): Avail => ({ allowed: false, reason });

/**
 * The one rule set for every place a scan is offered (the Overview, the stale
 * item, the Projects card, the scan history, the run page, the wizard's footer;
 * M2f-3 plan section 4.10, SCN-7 / CN-6). The project's state comes first (not
 * set up, importing, folder missing, access lost, an unsupported source, a dead
 * platform link), then the caller's role, then what stops a full scan only (a
 * linked project, a hard-stopped budget, no key for the describe model).
 *
 * `analyzeMissingKey` is the scan estimate's `missing_key` (the describe task's
 * provider without a key); the details payload's own `missing_key` may name the
 * Chat provider instead, so it is not read here.
 */
export function scanAvailability(
  p: ProjectSummary | ProjectDetails,
  { analyzeMissingKey = null }: { analyzeMissingKey?: string | null } = {},
): ScanAvailability {
  const linked = p.source === "platform";
  const linkStatus = linked ? p.link?.status : undefined;
  let blocked: Avail | null = null;
  let retryImport = false;
  if (p.importing) {
    if (p.running_scan) blocked = no("The import is still running");
    else retryImport = true;
  } else if (!p.initialized) blocked = no("Finish setting up this project first");
  else if (p.root_status === "missing") blocked = no("The project folder is missing");
  else if (p.root_status === "not_git") blocked = no("The project folder is no longer a git repository");
  else if (p.access_lost) blocked = no("GitHub no longer lets WhyGraph read this repository");
  else if (p.source_supported === false) blocked = no("This source is no longer supported in local mode");
  else if (linkStatus === "revoked") blocked = no("Reconnect the project to its platform first");
  else if (linkStatus === "removed") blocked = no("The project was removed on the platform");

  const role = can(p, "project.scan") ? OK : no("Needs the project Contributor role");
  const quick = blocked ?? role;

  let full: Avail;
  if (retryImport) full = no("Finish the import first");
  else if (blocked) full = blocked;
  else if (linked) full = no("Descriptions run on the platform");
  else if (!can(p, "project.scan_full") || p.llm_block === "role") full = no("Needs the project Admin role");
  else if (p.llm_block === "budget_exceeded") full = no("Monthly budget reached");
  else if (analyzeMissingKey) full = no(`No ${providerLabel(analyzeMissingKey)} key`);
  else full = OK;

  return { quick, full, retryImport };
}
