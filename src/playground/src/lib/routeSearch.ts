import type { ScanRunStatus } from "../api";

// The query strings of the routes in `router.tsx`, validated in one place so a
// page reads typed values (`useSearch({ from: ... })`) and never re-parses the
// URL. Every validator returns each of its keys explicitly (even as
// `undefined`): a match's search is the raw search overlaid with the validated
// one, so an omitted key would let the unvalidated value through. Values arrive
// as strings (`parseSearch`) or, from a programmatic `navigate`, as the typed
// value itself; both are accepted.

const text = (v: unknown, max = 200): string | undefined =>
  typeof v === "string" && v !== "" && v.length <= max ? v : undefined;

function oneOf<T extends string>(allowed: readonly T[], v: unknown): T | undefined {
  return allowed.find((a) => a === v);
}

/** A comma list (`"ok,failed"`) or an array, filtered to `allowed`, de-duplicated; empty -> undefined. */
function listOf<T extends string>(allowed: readonly T[], v: unknown): T[] | undefined {
  const raw = Array.isArray(v) ? v : typeof v === "string" ? v.split(",") : [];
  const out: T[] = [];
  for (const item of raw) {
    const hit = oneOf(allowed, typeof item === "string" ? item.trim() : item);
    if (hit && !out.includes(hit)) out.push(hit);
  }
  return out.length ? out : undefined;
}

function positiveInt(v: unknown): number | undefined {
  const n = typeof v === "number" ? v : typeof v === "string" && /^\d{1,15}$/.test(v) ? Number(v) : NaN;
  return Number.isSafeInteger(n) && n > 0 ? n : undefined;
}

// ---- / (Projects) -------------------------------------------------------------------

export const PROJECT_SORTS = ["name", "scanned", "status", "cost"] as const;
/** The Projects list order: Name, Last scanned, Status, Cost (§4.10). */
export type ProjectSort = (typeof PROJECT_SORTS)[number];

/** `/?q=&sort=`: the client-side search text and order of the Projects list. */
export interface ProjectsSearch {
  q?: string;
  sort?: ProjectSort;
}

export function validateProjectsSearch(search: Record<string, unknown>): ProjectsSearch {
  return { q: text(search.q), sort: oneOf(PROJECT_SORTS, search.sort) };
}

// ---- /p/$slug/init (the add wizard after Source) ---------------------------------------

export const INIT_STEPS = ["setup", "configure"] as const;
/** The wizard step after Source: Set up (Initialize) or Configure. */
export type InitStep = (typeof INIT_STEPS)[number];

/** The step names before M2f-3, still in old links and bookmarks. */
const LEGACY_INIT_STEPS: Record<string, InitStep> = { initialize: "setup", scan: "configure" };

/** `/p/$slug/init?step=&run=`: the step and the first scan's run id. */
export interface InitSearch {
  step?: InitStep;
  run?: number;
}

/**
 * The init route's search. An old `?step=initialize` / `?step=scan` maps to
 * `setup` / `configure`, and the router rewrites the address to match, so an old
 * link is a redirect.
 */
export function validateInitSearch(search: Record<string, unknown>): InitSearch {
  const step = oneOf(INIT_STEPS, search.step) ?? (typeof search.step === "string" ? LEGACY_INIT_STEPS[search.step] : undefined);
  return { step, run: positiveInt(search.run) };
}

// ---- /p/$slug/scans (history filters) ------------------------------------------------

/** `models.py` `SCAN_STATUSES`. */
export const SCAN_STATUSES = [
  "queued",
  "running",
  "ok",
  "failed",
  "interrupted",
  "cancelled",
] as const satisfies readonly ScanRunStatus[];
/** `models.py` `SCAN_TRIGGERS`. */
export const SCAN_TRIGGERS = ["initial", "manual", "describe", "hook", "poll", "sync", "push", "reconcile"] as const;
export type ScanTrigger = (typeof SCAN_TRIGGERS)[number];
export const SCAN_TYPES = ["full", "quick", "sync"] as const;
/** The history's Type filter (`GET .../scans?type=`): full, quick or a sync that only fetched. */
export type ScanType = (typeof SCAN_TYPES)[number];

/**
 * `/p/$slug/scans?status=&trigger=&type=&requester=`: the history filters, in the
 * shape `projectApi(slug).scans()` takes (`status` and `trigger` are comma lists
 * in the URL). `requester` is a user uid or `system`.
 */
export interface ScansSearch {
  status?: ScanRunStatus[];
  trigger?: ScanTrigger[];
  type?: ScanType;
  requester?: string;
}

export function validateScansSearch(search: Record<string, unknown>): ScansSearch {
  const requester = text(search.requester, 64);
  return {
    status: listOf(SCAN_STATUSES, search.status),
    trigger: listOf(SCAN_TRIGGERS, search.trigger),
    type: oneOf(SCAN_TYPES, search.type),
    requester: requester && /^[A-Za-z0-9_-]+$/.test(requester) ? requester : undefined,
  };
}

// ---- settings sections (§0.3 #22) -------------------------------------------------------
// Every id a settings page's section list renders, so each one deep-links (SET-1).

export const PROJECT_SETTINGS_SECTIONS = [
  "general",
  "models",
  "github",
  "hooks",
  "budgets",
  "agents",
  "access",
  "connections",
  "danger",
] as const;
export type ProjectSettingsSection = (typeof PROJECT_SETTINGS_SECTIONS)[number];
export const ORG_SETTINGS_SECTIONS = ["general", "models", "github", "limits", "budgets", "danger"] as const;
export type OrgSettingsSection = (typeof ORG_SETTINGS_SECTIONS)[number];

/** `/p/$slug/settings?section=`: the section to scroll to. */
export interface ProjectSettingsSearch {
  section?: ProjectSettingsSection;
}
/** `/settings?section=` (org / global settings). */
export interface OrgSettingsSearch {
  section?: OrgSettingsSection;
}

export function validateProjectSettingsSearch(search: Record<string, unknown>): ProjectSettingsSearch {
  return { section: oneOf(PROJECT_SETTINGS_SECTIONS, search.section) };
}

export function validateOrgSettingsSearch(search: Record<string, unknown>): OrgSettingsSearch {
  return { section: oneOf(ORG_SETTINGS_SECTIONS, search.section) };
}

// ---- base-host pages (production) -------------------------------------------------------

const ORG_SLUG = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/**
 * `/signin`, `/orgs` and `/account` on the base host: `next` (where sign-in
 * returns to), `stay` (`?stay=1`: the org picker does not auto-open the only
 * org) and `from` (the org slug the visitor came from, for "Back to <org>"; the
 * page still checks it is one of the caller's orgs).
 */
export interface BaseSearch {
  next?: string;
  stay?: boolean;
  from?: string;
}

export function validateBaseSearch(search: Record<string, unknown>): BaseSearch {
  const stay = search.stay === true || search.stay === "1" || search.stay === "true";
  const from = text(search.from, 63);
  return { next: text(search.next, Infinity), stay: stay || undefined, from: from && ORG_SLUG.test(from) ? from : undefined };
}
