export interface Crumb {
  label: string;
  /** Router location for a link crumb; the last crumb (the current page) has none. */
  to?: { to: string; params?: Record<string, string> };
}

const PROJECT_PAGES: Record<string, string> = {
  explorer: "Explorer",
  chat: "Chat",
  scans: "Scans",
  settings: "Settings",
  init: "Set up",
};

/**
 * Breadcrumbs for a pathname, in the shape of the §4.12.4 header:
 * `Projects / whygraph / Explorer`. Pure, so the route grammar is testable
 * without rendering. `memberName` labels a Usage & cost member drill-down
 * (`/usage/members/<uid>`); without it the uid shows.
 */
export function buildCrumbs(pathname: string, projectName?: string, memberName?: string): Crumb[] {
  const parts = pathname.split("/").filter(Boolean);
  const projects: Crumb = { label: "Projects", to: { to: "/" } };

  if (parts[0] === "p" && parts[1]) {
    const slug = decodeURIComponent(parts[1]);
    const home: Crumb = { label: projectName ?? slug, to: { to: "/p/$slug", params: { slug } } };
    const page = parts[2];
    if (!page) return [projects, { label: home.label }];
    const label = PROJECT_PAGES[page] ?? page;
    const pageCrumb: Crumb = {
      label,
      to: { to: `/p/$slug/${page}`, params: { slug } },
    };
    if (page === "scans" && parts[3]) {
      return [projects, home, pageCrumb, { label: `Run #${parts[3]}` }];
    }
    return [projects, home, { label }];
  }
  if (parts[0] === "projects" && parts[1] === "new") return [projects, { label: "Add project" }];
  if (parts[0] === "settings") return [{ label: "Settings" }];
  if (parts[0] === "members") return [{ label: "Members" }];
  if (parts[0] === "audit") return [{ label: "Audit log" }];
  if (parts[0] === "usage") {
    const usage: Crumb = { label: "Usage & cost", to: { to: "/usage" } };
    if (parts[1] === "me") return [usage, { label: "My usage" }];
    if (parts[1] === "members" && parts[2]) {
      return [usage, { label: memberName ?? decodeURIComponent(parts[2]) }];
    }
    return [{ label: "Usage & cost" }];
  }
  if (parts[0] === "setup") return [{ label: "Setup" }];
  return [{ label: "Projects" }];
}
