import type { ProjectSummary } from "../api";
import { projectHealth, type ProjectStatus } from "./projectHealth";

export type { ProjectStatus, StatusKey, StatusTone } from "./projectHealth";

/**
 * The status pill of a project (the Projects card, the Overview header, the
 * switcher's dot): the first item of {@link projectHealth}'s precedence that
 * names one, in the vocabulary of plan section 0.3 #38 - Ready, Scanning,
 * Importing, Needs setup, Behind, Scan failed, Import failed, Folder missing,
 * No access, Link revoked, Stopped (budget), Not supported - plus "Not scanned
 * yet" for a set-up project whose first scan has not run.
 */
export function projectStatus(p: ProjectSummary): ProjectStatus {
  return projectHealth(p).status;
}

export { timeAgo } from "./format";
