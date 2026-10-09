import type { ComponentType, ReactNode } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  ActivityIcon,
  ArrowLeftIcon,
  ChevronsUpDownIcon,
  CircleDollarSignIcon,
  LayoutDashboardIcon,
  LayoutGridIcon,
  NetworkIcon,
  SearchIcon,
  SlidersHorizontalIcon,
  WrenchIcon,
  ScrollTextIcon,
  UsersIcon,
} from "lucide-react";
import { portalApi, portalKey, projectKey, type PortalState, type ProjectSummary } from "../../api";
import { baseHostOf, canOwn, isProduction, useSignOut } from "../../lib/identity";
import { hardNavigate } from "../../lib/navigation";
import { can } from "../../lib/permissions";
import { useUi } from "../../store";
import { ThemeToggle } from "../ThemeToggle";
import { ChatsSection } from "./ChatsSection";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";
import { cn } from "@/lib/utils";

// The app-shell sidebar (§4.12.4, M2f-3 §4.6). Two scopes: portal (Projects,
// Settings, and Members in production) and project (Overview, Explorer, Scans,
// Settings, then the Chats section), with the project switcher on top and ⌘K /
// theme / user / version at the bottom. Four stacked blocks: the fixed top, the
// fixed nav, the Chats section (the only part that scrolls) and the footer; when
// the fixed blocks leave the Chats section less than four rows (a short window, a
// landscape phone) the whole sidebar scrolls instead, so the nav is never clipped.

interface NavItem {
  label: string;
  icon: ComponentType<{ className?: string }>;
  to: string;
  params?: Record<string, string>;
  exact?: boolean;
}

const ITEM =
  "flex h-8 items-center gap-2.5 rounded-md px-2 text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring data-[status=active]:bg-accent data-[status=active]:font-medium data-[status=active]:text-foreground";

function NavLink({ item }: { item: NavItem }) {
  const setNavOpen = useUi((s) => s.setNavOpen);
  return (
    <Link
      to={item.to}
      params={item.params}
      activeOptions={{ exact: item.exact ?? false }}
      className={ITEM}
      onClick={() => setNavOpen(false)}
    >
      <item.icon className="size-4" />
      {item.label}
    </Link>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="px-2 pb-1.5 pt-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        {title}
      </div>
      {children}
    </div>
  );
}

/**
 * The project nav. A linked project (M2e) has no local data, so no Explorer here
 * (its Explorer and Chat live on the platform). A project that is not set up yet
 * lists only "Continue setup" (the wizard) and Settings (NAV-6); one still being
 * imported lists Overview, Scans (the clone streams there) and Settings. Chat is
 * not an item: the Chats section below the nav is its entry point.
 */
function projectItems(
  slug: string,
  { linked, initialized, importing }: { linked: boolean; initialized: boolean; importing: boolean },
): NavItem[] {
  const params = { slug };
  const overview: NavItem = { label: "Overview", icon: LayoutDashboardIcon, to: "/p/$slug", params, exact: true };
  const scans: NavItem = { label: "Scans", icon: ActivityIcon, to: "/p/$slug/scans/{-$runId}", params };
  const settings: NavItem = { label: "Settings", icon: SlidersHorizontalIcon, to: "/p/$slug/settings", params };
  if (importing) return [overview, scans, settings];
  if (!initialized) {
    return [{ label: "Continue setup", icon: WrenchIcon, to: "/p/$slug/init", params }, settings];
  }
  return [
    overview,
    ...(linked ? [] : [{ label: "Explorer", icon: NetworkIcon, to: "/p/$slug/explorer", params } as NavItem]),
    scans,
    settings,
  ];
}

const PORTAL_ITEMS: NavItem[] = [
  { label: "Projects", icon: LayoutGridIcon, to: "/", exact: true },
  { label: "Settings", icon: SlidersHorizontalIcon, to: "/settings" },
];

// Production only (M2d-1), for every role: a reader and a member can read the list.
const MEMBERS_ITEM: NavItem = { label: "Members", icon: UsersIcon, to: "/members" };

// Production, owners only (`org.audit`).
const AUDIT_ITEM: NavItem = { label: "Audit log", icon: ScrollTextIcon, to: "/audit" };

/**
 * Usage & cost (M2f-2): the org's page for `org.usage` callers (owners, org admins,
 * readers; local mode's user), else a production member's own usage, else none.
 */
function usageItem(state: PortalState | undefined): NavItem | null {
  if (state?.usage?.org) return { label: "Usage & cost", icon: CircleDollarSignIcon, to: "/usage" };
  if (state?.usage?.me) return { label: "Usage & cost", icon: CircleDollarSignIcon, to: "/usage/me" };
  return null;
}

function ProjectSwitcher({ slug, name }: { slug?: string; name?: string }) {
  const navigate = useNavigate();
  const production = isProduction(useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state }).data);
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  const label = slug ? (name ?? slug) : "All projects";
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label="Switch project"
        className="flex h-9 w-full items-center gap-2 rounded-lg border border-border bg-background px-2 text-left text-[13px] outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="flex size-[18px] shrink-0 items-center justify-center rounded bg-muted text-[11px] font-semibold">
          {slug ? label.charAt(0).toUpperCase() : "*"}
        </span>
        <span className="min-w-0 flex-1 truncate font-medium">{label}</span>
        <ChevronsUpDownIcon className="size-3.5 shrink-0 text-muted-foreground" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-56">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Projects</DropdownMenuLabel>
          {projects.data?.projects.map((p) => (
            <DropdownMenuItem
              key={p.slug}
              onClick={() => navigate({ to: "/p/$slug", params: { slug: p.slug } })}
            >
              <span className="flex-1 truncate">{p.name}</span>
              {p.slug === slug && <span className="text-xs text-muted-foreground">current</span>}
            </DropdownMenuItem>
          ))}
          {projects.isSuccess && projects.data.projects.length === 0 && (
            <div className="px-1.5 py-1 text-xs text-muted-foreground">No projects yet</div>
          )}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => navigate({ to: "/" })}>All projects</DropdownMenuItem>
        {!production && (
          <DropdownMenuItem onClick={() => navigate({ to: "/projects/new" })}>
            Add project
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function Sidebar({ slug, projectName }: { slug?: string; projectName?: string }) {
  const setPaletteOpen = useUi((s) => s.setPaletteOpen);
  const setNavOpen = useUi((s) => s.setNavOpen);
  const state = useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state });
  const user = state.data?.user;
  const version = state.data?.version;
  const production = isProduction(state.data);
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  const listed = projects.data?.projects.find((p) => p.slug === slug);
  // The project layout's own query (fresher: a chat's budget stop refetches it), else the list row.
  const details = useQuery({
    queryKey: projectKey(slug ?? "", "project"),
    queryFn: () => portalApi.project(slug ?? ""),
    enabled: false,
  });
  const current: ProjectSummary | undefined = (slug && details.data) || listed;
  const linked = current?.source === "platform";
  const initialized = !!current?.initialized;
  const importing = !!current?.importing;
  const canChat = can(current, "project.chat");
  const showChats = !!slug && canChat && !linked && initialized && !importing;
  const signOut = useSignOut();
  const base = state.data?.base_url?.replace(/\/$/, "") ?? "";
  const usage = usageItem(state.data);

  return (
    <nav
      aria-label="Main"
      className="flex h-full w-60 shrink-0 flex-col overflow-y-auto border-r border-sidebar-border bg-sidebar text-sidebar-foreground"
    >
      <div className="flex shrink-0 flex-col gap-3 px-3 pb-3 pt-4">
        <div className="flex items-center gap-2 px-1.5">
          <div className="flex size-[22px] items-center justify-center rounded-md bg-primary">
            <NetworkIcon className="size-3.5 text-primary-foreground" aria-hidden />
          </div>
          <span className="font-semibold tracking-tight">WhyGraph</span>
          <span className="ml-auto rounded border border-border px-1.5 py-px text-[11px] text-muted-foreground">
            {production ? (state.data?.org?.name ?? baseHostOf(state.data?.base_url)) : (state.data?.mode ?? "local")}
          </span>
        </div>
        <ProjectSwitcher slug={slug} name={projectName} />
      </div>

      <div className="flex shrink-0 flex-col gap-0.5 px-3 py-1">
        {slug ? (
          <>
            <Section title="Project">
              {projectItems(slug, { linked, initialized, importing }).map((item) => (
                <NavLink key={item.label} item={item} />
              ))}
            </Section>
            <div className="mx-2 my-2.5 h-px bg-border" />
            <Link to="/" className={ITEM} onClick={() => setNavOpen(false)}>
              <ArrowLeftIcon className="size-4" />
              All projects
            </Link>
          </>
        ) : (
          <Section title="Portal">
            {[
              ...(production
                ? [...PORTAL_ITEMS, MEMBERS_ITEM, ...(canOwn(state.data?.org?.role ?? undefined) ? [AUDIT_ITEM] : [])]
                : PORTAL_ITEMS),
              ...(usage ? [usage] : []),
            ].map((item) => (
              <NavLink key={item.label} item={item} />
            ))}
          </Section>
        )}
      </div>

      {showChats ? (
        <ChatsSection slug={slug} budgetStopped={current?.llm_block === "budget_exceeded"} />
      ) : (
        <div className="flex-1" />
      )}

      <div className="flex shrink-0 flex-col gap-1 border-t border-sidebar-border p-3">
        <button
          type="button"
          onClick={() => setPaletteOpen(true)}
          className={cn(ITEM, "w-full cursor-pointer text-left")}
        >
          <SearchIcon className="size-4" />
          <span className="flex-1">Search</span>
          <kbd className="rounded border border-border bg-background px-1.5 py-px font-mono text-[11px]">
            ⌘K
          </kbd>
        </button>
        <ThemeToggle />
        {production ? (
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label="Account menu"
              className="flex items-center gap-2.5 rounded-md px-2 pb-0.5 pt-2 text-left outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-primary-foreground">
                {(user?.display_name ?? "?").charAt(0).toUpperCase()}
              </span>
              <div className="flex min-w-0 flex-1 flex-col leading-tight">
                <span className="truncate text-[13px] font-medium">{user?.display_name}</span>
                {version && <span className="font-mono text-[11px] text-muted-foreground">v{version}</span>}
              </div>
              <ChevronsUpDownIcon className="size-3.5 shrink-0 text-muted-foreground" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-56">
              <DropdownMenuItem onClick={() => void hardNavigate(`${base}/account`)}>Account</DropdownMenuItem>
              <DropdownMenuItem onClick={() => void hardNavigate(`${base}/orgs`)}>
                Switch organization
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => void hardNavigate(`${base}/orgs/new`)}>
                Create organization
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={() => void signOut()}>Sign out</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        ) : (
          <div className="flex items-center gap-2.5 px-2 pb-0.5 pt-2">
            <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-primary-foreground">
              {(user?.display_name ?? "?").charAt(0).toUpperCase()}
            </span>
            <div className="flex min-w-0 flex-col leading-tight">
              <span className="truncate text-[13px] font-medium">{user?.display_name ?? "Local user"}</span>
              {version && <span className="font-mono text-[11px] text-muted-foreground">v{version}</span>}
            </div>
          </div>
        )}
      </div>
    </nav>
  );
}
