import {
  LayoutDashboardIcon,
  FolderGit2Icon,
  LayoutGridIcon,
  ScrollTextIcon,
  SlidersHorizontalIcon,
  UsersIcon,
} from "lucide-react";
import type { NavItem } from "../shell/Sidebar";

/** The admin nav (plan 59, §4.14); the palette's "Go to" group reads it too. */
export const ADMIN_NAV: NavItem[] = [
  { label: "Dashboard", icon: LayoutDashboardIcon, to: "/admin", exact: true },
  { label: "Organizations", icon: LayoutGridIcon, to: "/admin/orgs" },
  { label: "Projects", icon: FolderGit2Icon, to: "/admin/projects" },
  { label: "Users", icon: UsersIcon, to: "/admin/users" },
  { label: "Audit", icon: ScrollTextIcon, to: "/admin/audit" },
  { label: "Configuration", icon: SlidersHorizontalIcon, to: "/admin/config" },
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
 * uid until its own query has loaded: `names` carries the loaded org or user name.
 */
export function adminCrumbs(
  pathname: string,
  names: { org?: string; user?: string; project?: string } = {},
): { heading: string; crumbs: AdminCrumb[] } {
  const parts = pathname.replace(/\/+$/, "").split("/").filter(Boolean).slice(1).map(decodeURIComponent);
  const root: AdminCrumb = { label: "Administration", to: "/admin" };
  if (parts.length === 0) return { heading: "Dashboard", crumbs: [{ label: "Administration" }] };
  const module = parts[0];
  const heading = MODULES[module];
  if (!heading) return { heading: "Not found", crumbs: [root, { label: "Not found" }] };
  if (parts.length === 1) return { heading, crumbs: [root, { label: heading }] };
  if (module === "projects" && parts.length === 3) {
    const name = names.project ?? parts[2];
    return {
      heading: name,
      crumbs: [root, { label: heading, to: "/admin/projects" }, { label: `${parts[1]} / ${name}` }],
    };
  }
  const name = (module === "orgs" ? names.org : module === "users" ? names.user : undefined) ?? parts[1];
  return {
    heading: name,
    crumbs: [root, { label: heading, to: `/admin/${module}` }, { label: name }],
  };
}
