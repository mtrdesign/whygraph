import { projectKey } from "../../api";

export interface Crumb {
  label: string;
  /**
   * Router location for a link crumb. The last crumb (the current page) has none,
   * and a parent with none is plain text (a parent that would only redirect back).
   */
  to?: { to: string; params?: Record<string, string> };
}

/** What `buildCrumbs` cannot read from the path: names and titles from the query cache. */
export interface CrumbContext {
  /** The project's display name (the slug shows without it). */
  projectName?: string;
  /** A Usage & cost member drill-down's name (`/usage/members/<uid>`; the uid shows without it). */
  memberName?: string;
  /** The open chat session's title (`/p/<slug>/chat/<id>`). */
  chatTitle?: string;
  /** The open scan run's title (`/p/<slug>/scans/<id>`); never "Run #5" (§4.11). */
  runTitle?: string;
  /** The add wizard's step (`/p/<slug>/init?step=`), so the crumb matches the page title. */
  wizardStep?: "setup" | "configure";
  /** The caller may open the org's Usage & cost page; otherwise `/usage` only redirects to `/usage/me`. */
  orgUsage?: boolean;
}

const PROJECT_PAGES: Record<string, string> = {
  explorer: "Explorer",
  chat: "Chat",
  scans: "Scans",
  settings: "Settings",
};

const WIZARD_STEPS: Record<NonNullable<CrumbContext["wizardStep"]>, string> = {
  setup: "Set up",
  configure: "Configure",
};

const NOT_FOUND: Crumb = { label: "Not found" };

/**
 * Breadcrumbs for a pathname, in the shape of the §4.12.4 header:
 * `Projects / whygraph / Explorer`. Pure, so the route grammar is testable
 * without rendering; the titles the path does not carry come in `ctx`.
 */
export function buildCrumbs(pathname: string, ctx: CrumbContext = {}): Crumb[] {
  const parts = pathname.split("/").filter(Boolean);
  const projects: Crumb = { label: "Projects", to: { to: "/" } };

  if (parts[0] === "p" && parts[1]) {
    const slug = decodeURIComponent(parts[1]);
    const name = ctx.projectName ?? slug;
    const home: Crumb = { label: name, to: { to: "/p/$slug", params: { slug } } };
    const page = parts[2];
    if (!page) return [projects, { label: name }];
    if (page === "init") {
      // The add wizard: its crumb is the step, as the page title is.
      const step = ctx.wizardStep ? WIZARD_STEPS[ctx.wizardStep] : undefined;
      return step ? [projects, { label: "Add project" }, { label: step }] : [projects, { label: "Add project" }];
    }
    const label = PROJECT_PAGES[page];
    if (!label) return [projects, home, NOT_FOUND];
    if (page === "scans" && parts[3]) {
      return [projects, home, { label, to: { to: "/p/$slug/scans/{-$runId}", params: { slug } } }, { label: ctx.runTitle ?? "Scan run" }];
    }
    if (page === "chat" && parts[3] && ctx.chatTitle) {
      // "Chat" alone would start a new chat, so it is not a link.
      return [projects, home, { label }, { label: ctx.chatTitle }];
    }
    return [projects, home, { label }];
  }
  if (parts.length === 0) return [{ label: "Projects" }];
  const path = parts.join("/");
  if (path === "projects/new") return [projects, { label: "Add project" }];
  if (path === "link") return [projects, { label: "Link a project" }];
  if (path === "connect/callback") return [{ label: "Connect" }];
  if (path === "settings") return [{ label: "Settings" }];
  if (path === "members") return [{ label: "Members" }];
  if (path === "audit") return [{ label: "Audit log" }];
  if (parts[0] === "usage") {
    // A member's `/usage` redirects to `/usage/me`, so their parent crumb is plain text (BUG-19).
    const usage: Crumb = ctx.orgUsage ? { label: "Usage & cost", to: { to: "/usage" } } : { label: "Usage & cost" };
    if (path === "usage") return [{ label: "Usage & cost" }];
    if (path === "usage/me") return [usage, { label: "My usage" }];
    if (parts[1] === "members" && parts[2] && parts.length === 3) {
      return [usage, { label: ctx.memberName ?? decodeURIComponent(parts[2]) }];
    }
    return [NOT_FOUND];
  }
  if (path === "setup") return [{ label: "Setup" }];
  return [NOT_FOUND];
}

/**
 * The page part of the document title for these crumbs: the last crumb, except
 * a project's own page, which is its Overview.
 */
export function pageTitle(crumbs: Crumb[], pathname: string): string {
  const parts = pathname.split("/").filter(Boolean);
  if (parts[0] === "p" && parts[1] && !parts[2]) return "Overview";
  return crumbs[crumbs.length - 1]?.label ?? "Projects";
}

/**
 * The query key of one scan run (`GET /api/projects/<slug>/scans/<id>`). The run
 * page caches the run here; the header reads it for the run's crumb title.
 */
export function scanRunKey(slug: string, runId: number): readonly unknown[] {
  return projectKey(slug, "scan", runId);
}
