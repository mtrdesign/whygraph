import {
  LayoutDashboardIcon,
  LayoutGridIcon,
  ScrollTextIcon,
  UsersIcon,
} from "lucide-react";
import type { NavItem } from "../shell/Sidebar";

/**
 * The admin nav (plan 59, §4.14). A module that does not exist in this release
 * (Projects, Configuration) is left out here, and so out of the palette too; its
 * route renders "Not available yet" until the phase that builds it.
 */
export const ADMIN_NAV: NavItem[] = [
  { label: "Dashboard", icon: LayoutDashboardIcon, to: "/admin", exact: true },
  { label: "Organizations", icon: LayoutGridIcon, to: "/admin/orgs" },
  { label: "Users", icon: UsersIcon, to: "/admin/users" },
  { label: "Audit", icon: ScrollTextIcon, to: "/admin/audit" },
];

/** One crumb of the admin header: `to` is absent on the last one. */
export interface AdminCrumb {
  label: string;
  to?: string;
}

const MODULES: Record<string, string> = {
  orgs: "Organizations",
  users: "Users",
  projects: "Projects",
  audit: "Audit log",
  config: "Platform configuration",
  account: "Account",
};

/**
 * The page's `h1`, its crumbs and the document-title page name, from the path
 * (§4.14 "Headings, crumbs and titles"). A detail page is named by its slug or
 * uid until a later phase loads its real name.
 */
export function adminCrumbs(pathname: string): { heading: string; crumbs: AdminCrumb[] } {
  const parts = pathname.replace(/\/+$/, "").split("/").filter(Boolean).slice(1).map(decodeURIComponent);
  const root: AdminCrumb = { label: "Administration", to: "/admin" };
  if (parts.length === 0) return { heading: "Dashboard", crumbs: [{ label: "Administration" }] };
  const module = parts[0];
  const heading = MODULES[module];
  if (!heading) return { heading: "Not found", crumbs: [root, { label: "Not found" }] };
  if (parts.length === 1) return { heading, crumbs: [root, { label: heading }] };
  if (module === "projects" && parts.length === 3) {
    return {
      heading: parts[2],
      crumbs: [root, { label: heading, to: "/admin/projects" }, { label: `${parts[1]} / ${parts[2]}` }],
    };
  }
  return {
    heading: parts[1],
    crumbs: [root, { label: heading, to: `/admin/${module}` }, { label: parts[1] }],
  };
}
