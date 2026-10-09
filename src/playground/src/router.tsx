import { useEffect } from "react";
import {
  Link,
  Navigate,
  Outlet,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  redirect,
  useParams,
  useRouteContext,
  useRouterState,
  useSearch,
  type RouterHistory,
} from "@tanstack/react-router";
import { useQuery, type QueryClient } from "@tanstack/react-query";
import {
  ApiError,
  portalApi,
  portalKey,
  projectApi,
  projectKey,
  setBaseUrl,
  setErrorMode,
  type PortalState,
  type ProjectDetails,
} from "./api";
import { projectProblem } from "./lib/errors";
import { can } from "./lib/permissions";
import { getLastProject, setLastProject } from "./lib/lastProject";
import { ProjectProvider } from "./lib/project";
import { canAdmin, canOwn, isProduction, isSafeNext, signInUrl, usePortalState } from "./lib/identity";
import { hardNavigate } from "./lib/navigation";
import { Alert, AlertDescription, AlertTitle } from "./components/ui/alert";
import { safeLinkNext } from "./lib/linkNext";
import { BudgetNotice } from "./components/portal/BudgetNotice";
import { LinkedElsewhere } from "./components/portal/LinkedActions";
import { AppShell } from "./components/shell/AppShell";
import { useGlobalShortcuts } from "./components/shell/shortcuts";
import { CommandPalette } from "./components/CommandPalette";
import { ChatView } from "./components/chat/ChatView";
import { ExplorerPage } from "./pages/ExplorerPage";
import { ProjectHome } from "./pages/ProjectHome";
import { ProjectsPage } from "./pages/ProjectsPage";
import { AddProjectPage, type NewProjectSearch } from "./pages/AddProjectPage";
import { InitProjectPage } from "./pages/InitProjectPage";
import { SetupPage } from "./pages/SetupPage";
import { GlobalSettingsPage } from "./pages/GlobalSettingsPage";
import { AuditPage } from "./pages/AuditPage";
import { MembersPage } from "./pages/MembersPage";
import { MemberUsagePage } from "./pages/MemberUsagePage";
import { UsagePage, validateRangeSearch, validateUsageSearch } from "./pages/UsagePage";
import { ProjectSettingsPage } from "./pages/ProjectSettingsPage";
import { ScansPage } from "./pages/ScansPage";
import { ConnectPage, type ConnectSearch } from "./pages/ConnectPage";
import { ConnectCallbackPage, type CallbackSearch } from "./pages/ConnectCallbackPage";
import { LinkPage, type LinkSearch } from "./pages/LinkPage";
import { AccountPage } from "./pages/AccountPage";
import { AdminPage } from "./pages/AdminPage";
import { BootstrapPage } from "./pages/BootstrapPage";
import { GitHubAppCallbackPage, type GitHubAppCallbackParams } from "./pages/GitHubAppCallbackPage";
import { GitHubCallbackPage } from "./pages/GitHubCallbackPage";
import { CreateOrgPage } from "./pages/CreateOrgPage";
import { NoOrgAccessPage } from "./pages/NoOrgAccessPage";
import { OrgPickerPage } from "./pages/OrgPickerPage";
import { ResetPasswordPage } from "./pages/ResetPasswordPage";
import { SessionNotReceivedPage } from "./pages/SessionNotReceivedPage";
import { SignInPage } from "./pages/SignInPage";
import {
  ImportingNotice,
  NotFoundPage,
  NotInitialized,
  ProblemAlert,
  ProjectUnavailable,
} from "./components/portal/EdgeStates";
import { PortalErrorPage } from "./components/state/PortalErrorPage";
import { QueryState } from "./components/state/QueryState";
import { PageSkeleton } from "./components/state/Skeletons";
import {
  validateBaseSearch,
  validateInitSearch,
  validateOrgSettingsSearch,
  validateProjectSettingsSearch,
  validateProjectsSearch,
  validateScansSearch,
} from "./lib/routeSearch";

// The route tree for §4.9, code-based (a generated `routeTree.gen.ts` would not
// exist yet when `tsc --noEmit` runs ahead of `vite build`).
//
//   /setup                               first run (no shell); production: the bootstrap
//   /signin /auth/github /auth/github-app /reset /orgs /orgs/new /admin /account /connect
//                                        production base host only (no AppShell)
//   /                                    Projects              ┐ portal layout
//   /projects/new                        add-project wizard    │ (sidebar: Projects,
//   /settings                            global settings       │  Settings, and
//   /members                             org members           │  Members, and the
//   /audit                               audit log (owners)    │  owners' Audit log, in production;
//   /usage, /usage/me, /usage/members/$uid  Usage & cost       ┘  Usage & cost when state.usage allows)
//   /p/$slug                             ProjectHome           ┐ project layout
//   /p/$slug/explorer?node=&file=        Explorer              │ (sidebar: Overview,
//   /p/$slug/chat/{-$id}                 Chat                  │  Explorer, Chat,
//   /p/$slug/scans/{-$runId}             Scans / run           │  Scans, Settings)
//   /p/$slug/settings, /p/$slug/init                           ┘
//   /explorer, /chat/{-$id}              legacy links -> last-used project

export interface RouterContext {
  queryClient: QueryClient;
}

// ---- search params ----------------------------------------------------------
//
// TanStack's default search codec is JSON: `?node=a.b` would round-trip as the
// string "a.b" but `?node=123` as a *number* and `?file=%22x%22` with stray
// quotes. Explorer deep links carry qualified names (`.`, `:`, `<>`) and paths
// (`/`), so both directions go through URLSearchParams and every value stays a
// plain string - the same encoding the pre-router `urlSync.ts` used.

export function parseSearch(search: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of new URLSearchParams(search.startsWith("?") ? search.slice(1) : search)) {
    out[key] = value;
  }
  return out;
}

export function stringifySearch(search: Record<string, unknown>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(search)) {
    if (value === undefined || value === null || value === "" || value === false) continue;
    // A flag is written `?stay=1`; an array (a filter list) joins with commas.
    params.set(key, value === true ? "1" : String(value));
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

const text = (v: unknown) => (typeof v === "string" && v !== "" ? v : undefined);

function validateExplorerSearch(search: Record<string, unknown>): { node?: string; file?: string } {
  const node = text(search.node);
  // A bare ?file= selects nothing, so it is only kept alongside a node.
  // Keys are returned explicitly (even as undefined): a match's search is the
  // raw location search overlaid with the validated one, so an omitted key would
  // let the unvalidated value show through.
  return { node, file: node ? text(search.file) : undefined };
}

// ---- last-used project ------------------------------------------------------

const projectsQuery = (queryClient: QueryClient) =>
  queryClient.fetchQuery({
    queryKey: portalKey("projects"),
    queryFn: portalApi.projects,
    staleTime: 10_000,
  });

/**
 * The remembered project's slug, or `null` when there is none or it no longer
 * exists. `lastProject` is only a hint: it is checked against the live project
 * list so a removed project can never redirect into a 404.
 */
export async function resolveLastProject(queryClient: QueryClient): Promise<string | null> {
  const slug = getLastProject();
  if (!slug) return null;
  const { projects } = await projectsQuery(queryClient);
  return projects.some((p) => p.slug === slug) ? slug : null;
}

// ---- root -------------------------------------------------------------------

// ---- production identity gate (M2c section 4.10) ----------------------------

// What the base host serves (everything else is the org tree, which lives on an
// org host), and the subset a signed-out visitor may open.
const BASE_PATHS = new Set([
  "/signin",
  "/auth/github",
  "/auth/github-app",
  "/setup",
  "/reset",
  "/orgs",
  "/orgs/new",
  "/admin",
  "/account",
  "/connect",
]);
const SIGNED_OUT_PATHS = new Set(["/signin", "/auth/github", "/reset"]);

/** A redirect target that may carry a query (`/signin?next=...`) as TanStack's `to` + `search`. */
function redirectTo(target: string) {
  const url = new URL(target, "http://placeholder.invalid");
  return { to: url.pathname, search: parseSearch(url.search), replace: true as const };
}

/**
 * Where the local portal's first-run gate sends this request, or `null` to render.
 * A `/link` request (a platform's "Open in my local WhyGraph") keeps its query:
 * it rides `/setup?next=` and is resumed once setup is done (`safeLinkNext`).
 */
export function setupRedirect(
  portal: PortalState,
  location: { pathname: string; search?: string },
): string | null {
  const onSetup = location.pathname === "/setup";
  if (!portal.setup_complete && !onSetup) {
    return location.pathname === "/link"
      ? `/setup?next=${encodeURIComponent(`/link${location.search ?? ""}`)}`
      : "/setup";
  }
  if (portal.setup_complete && onSetup) return safeLinkNext(location.search) ?? "/";
  return null;
}

/**
 * Where the base host sends this request instead of rendering it, or `null` to
 * render. `/signin` with a valid `next` while signed in is *not* redirected: the
 * route renders the "session not received" page there, which breaks the loop
 * org host (no cookie) -> sign-in -> picker -> org host.
 */
export function baseHostRedirect(portal: PortalState, pathname: string, href?: string): string | null {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  if (portal.bootstrap_required) return path === "/setup" ? null : "/setup";
  if (path === "/setup") return "/";
  if (!portal.user) {
    if (SIGNED_OUT_PATHS.has(path)) return null;
    // A consent request keeps its whole query across sign-in: `next` is the full
    // address, which the server accepts for the base host (`safe_redirect`).
    if (path === "/connect" && href) return `/signin?next=${encodeURIComponent(href)}`;
    return "/signin";
  }
  if (path === "/") return "/orgs";
  if (!BASE_PATHS.has(path)) return "/";
  if (path === "/admin" && !portal.user.is_instance_admin) return "/orgs";
  return null;
}

function RootLayout() {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  useGlobalShortcuts();
  // The portal's own database failed to open or migrate: nothing else can load.
  if (portal.error) return <PortalErrorPage error={portal.error} />;
  if (portal.mode === "production" && portal.host_kind === "org" && portal.user && !portal.org) {
    return <NoOrgAccessPage />;
  }
  return <Outlet />;
}

function RootNotFound() {
  const state = usePortalState();
  // The shell's sidebar calls /api/projects, which is a 404 on the base host.
  if (isProduction(state.data) && state.data?.host_kind === "base") return <NotFoundPage />;
  return (
    <AppShell>
      <NotFoundPage />
    </AppShell>
  );
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  // First-run gate: until setup is complete every page redirects to /setup, and
  // /setup itself is a dead end afterwards. `GET /api/portal/state` is public, so
  // it is the one call that works before a user exists.
  beforeLoad: async ({ context, location }) => {
    const portal = await context.queryClient.fetchQuery({
      queryKey: portalKey("state"),
      queryFn: portalApi.state,
      staleTime: 30_000,
    });
    const kind = portal.mode === "production" ? portal.host_kind : undefined;
    setErrorMode(portal.mode === "production" ? "production" : "local");
    setBaseUrl(!portal.error && kind && kind !== "local" ? (portal.base_url ?? null) : null);
    if (!portal.error && kind === "base") {
      const href = new URL(location.href, window.location.origin).href;
      const to = baseHostRedirect(portal, location.pathname, href);
      if (to) throw redirect(redirectTo(to));
    } else if (!portal.error && kind === "org" && portal.base_url) {
      const base = new URL(portal.base_url);
      if (!portal.user) {
        // `href` is the path and query; the org host's own origin completes it.
        const here = new URL(location.href, window.location.origin).href;
        await hardNavigate(signInUrl(portal.base_url, here));
      } else if (portal.org && BASE_PATHS.has(location.pathname)) {
        await hardNavigate(`${base.origin}${location.pathname}${location.searchStr ?? ""}`);
      }
    } else if (!portal.error) {
      const to = setupRedirect(portal, { pathname: location.pathname, search: location.searchStr });
      if (to) throw redirect(redirectTo(to));
    }
    return { portal };
  },
  component: RootLayout,
  errorComponent: ({ error, reset }) => <PortalErrorPage error={error} reset={reset} />,
  notFoundComponent: RootNotFound,
});

// One /setup route: local mode's first-run page, or production's bootstrap.
function SetupRoute() {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  return portal.mode === "production" ? <BootstrapPage /> : <SetupPage />;
}
const setupRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/setup",
  validateSearch: (search: Record<string, unknown>): { next?: string } => ({ next: text(search.next) }),
  component: SetupRoute,
});

// ---- base-host pages (production; no AppShell) ----------------------------------

// `next`, `stay` and `from` (lib/routeSearch.ts) on every base route that reads them.
const validateNext = validateBaseSearch;

function BaseLayout() {
  const state = usePortalState();
  const user = state.data?.user;
  if (!user) return <Outlet />;
  return (
    <>
      <nav aria-label="Account" className="flex items-center gap-4 border-b border-border px-6 py-2 text-sm">
        <Link to="/orgs" className="font-medium hover:underline">
          Organizations
        </Link>
        {user.is_instance_admin && (
          <Link to="/admin" className="hover:underline">
            Administration
          </Link>
        )}
        <Link to="/account" className="ml-auto hover:underline">
          {user.display_name}
        </Link>
      </nav>
      <Outlet />
    </>
  );
}

function SignInRoute() {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  const { next } = useSearch({ strict: false }) as { next?: string };
  const state = usePortalState().data ?? portal;
  if (state.user) {
    // Signed in: no `next` goes to the picker; a valid `next` means that address
    // did not receive the cookie, so say so instead of looping.
    if (isSafeNext(next, state.base_url)) return <SessionNotReceivedPage />;
    return <Navigate to="/orgs" replace />;
  }
  return <SignInPage />;
}

const baseLayout = createRoute({ getParentRoute: () => rootRoute, id: "base", component: BaseLayout });
const signInRoute = createRoute({
  getParentRoute: () => baseLayout,
  path: "/signin",
  validateSearch: validateNext,
  component: SignInRoute,
});
// GitHub's return address (M2d-1); a signed-in visitor may land here too (a new
// sign-in replaces the session).
const githubCallbackRoute = createRoute({
  getParentRoute: () => baseLayout,
  path: "/auth/github",
  validateSearch: (search: Record<string, unknown>): { code?: string; state?: string; error?: string } => ({
    code: text(search.code),
    state: text(search.state),
    error: text(search.error),
  }),
  component: GitHubCallbackPage,
});
// The GitHub App's return address (M2d-2): after a user authorization or an
// install, both started from an org's import page.
const githubAppCallbackRoute = createRoute({
  getParentRoute: () => baseLayout,
  path: "/auth/github-app",
  validateSearch: (search: Record<string, unknown>): GitHubAppCallbackParams => ({
    code: text(search.code),
    state: text(search.state),
    installation_id: text(search.installation_id),
    setup_action: text(search.setup_action),
    iss: text(search.iss),
    error: text(search.error),
  }),
  component: GitHubAppCallbackPage,
});
const resetRoute = createRoute({ getParentRoute: () => baseLayout, path: "/reset", component: ResetPasswordPage });
const orgsRoute = createRoute({
  getParentRoute: () => baseLayout,
  path: "/orgs",
  validateSearch: validateNext,
  component: OrgPickerPage,
});
const newOrgRoute = createRoute({ getParentRoute: () => baseLayout, path: "/orgs/new", component: CreateOrgPage });
const adminRoute = createRoute({ getParentRoute: () => baseLayout, path: "/admin", component: AdminPage });
// Local mode has no account (one implicit user): its settings are the portal's (BUG-10).
const accountRoute = createRoute({
  getParentRoute: () => baseLayout,
  path: "/account",
  validateSearch: validateNext,
  beforeLoad: ({ context }) => {
    const { portal } = context as { portal?: PortalState };
    if (portal && !portal.error && portal.mode !== "production") throw redirect({ to: "/settings", replace: true });
  },
  component: AccountPage,
});
// A local portal's consent request (M2e): signed-out visitors are sent to sign in
// first, with this whole address as `next`.
const connectRoute = createRoute({
  getParentRoute: () => baseLayout,
  path: "/connect",
  validateSearch: (search: Record<string, unknown>): ConnectSearch => ({
    redirect_uri: text(search.redirect_uri),
    code_challenge: text(search.code_challenge),
    code_challenge_method: text(search.code_challenge_method),
    state: text(search.state),
    client_name: text(search.client_name),
    org: text(search.org),
    project: text(search.project),
  }),
  component: ConnectPage,
});

// ---- portal layout ----------------------------------------------------------

function PortalLayout() {
  return (
    <AppShell>
      <Outlet />
      <CommandPalette />
    </AppShell>
  );
}

const portalLayout = createRoute({ getParentRoute: () => rootRoute, id: "portal", component: PortalLayout });
const projectsRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/",
  validateSearch: validateProjectsSearch,
  component: ProjectsPage,
});
// Local mode's single user may always add; in production only an org's owners
// and admins may (`org.add_project`), everyone else goes back to Projects.
function NewProjectRoute() {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  const state = usePortalState().data ?? portal;
  if (isProduction(state) && !canAdmin(state.org?.role ?? undefined)) return <Navigate to="/" replace />;
  return <AddProjectPage />;
}
const newProjectRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/projects/new",
  // `?source=platform` picks the platform source; `?link=` is a pending link (M2e).
  validateSearch: (search: Record<string, unknown>): NewProjectSearch => ({
    source: search.source === "platform" ? "platform" : undefined,
    link: text(search.link),
  }),
  component: NewProjectRoute,
});

// Linking a checkout to a platform project exists in local mode only.
function LocalOnly({ children }: { children: React.ReactNode }) {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  return isProduction(portal) ? <NotFoundPage /> : <>{children}</>;
}
const linkRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/link",
  validateSearch: (search: Record<string, unknown>): LinkSearch => ({
    platform: text(search.platform),
    org: text(search.org),
    project: text(search.project),
  }),
  component: () => (
    <LocalOnly>
      <LinkPage />
    </LocalOnly>
  ),
});
const connectCallbackRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/connect/callback",
  validateSearch: (search: Record<string, unknown>): CallbackSearch => ({
    code: text(search.code),
    state: text(search.state),
    iss: text(search.iss),
    error: text(search.error),
  }),
  component: () => (
    <LocalOnly>
      <ConnectCallbackPage />
    </LocalOnly>
  ),
});
const globalSettingsRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/settings",
  validateSearch: validateOrgSettingsSearch,
  component: GlobalSettingsPage,
});

// Org members exist only in production; local mode has one implicit user.
function MembersRoute() {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  return isProduction(portal) ? <MembersPage /> : <NotFoundPage kind="team-only" />;
}
const membersRoute = createRoute({ getParentRoute: () => portalLayout, path: "/members", component: MembersRoute });

// The audit log: production, owners only (`org.audit`).
function AuditRoute() {
  const state = usePortalState().data;
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  const current = state ?? portal;
  return isProduction(current) && canOwn(current.org?.role ?? undefined) ? <AuditPage /> : <NotFoundPage />;
}
const auditRoute = createRoute({ getParentRoute: () => portalLayout, path: "/audit", component: AuditRoute });

// Usage & cost (M2f-2). The org's page needs `org.usage` (`state.usage.org` is set:
// owners, org admins, readers, local mode's user); a production member, who has
// only their own usage, is sent to `/usage/me`.
function UsageRoute() {
  const state = usePortalState().data;
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  const usage = (state ?? portal).usage;
  if (usage?.org) return <UsagePage />;
  if (usage?.me) return <Navigate to="/usage/me" replace />;
  return <NotFoundPage />;
}
const usageRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/usage",
  validateSearch: validateUsageSearch,
  component: UsageRoute,
});
// My usage: production memberships only (`state.usage.me`).
function MyUsageRoute() {
  const state = usePortalState().data;
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  const current = state ?? portal;
  if (!isProduction(current)) return <NotFoundPage kind="team-only" />;
  return current.usage?.me ? <MemberUsagePage /> : <NotFoundPage />;
}
const myUsageRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/usage/me",
  validateSearch: validateRangeSearch,
  component: MyUsageRoute,
});
// One member's drill-down: production, `org.usage`.
function MemberUsageRoute() {
  const state = usePortalState().data;
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  const { uid } = useParams({ strict: false }) as { uid: string };
  const current = state ?? portal;
  if (!isProduction(current)) return <NotFoundPage kind="team-only" />;
  return current.usage?.org ? <MemberUsagePage key={uid} uid={uid} /> : <NotFoundPage />;
}
const memberUsageRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/usage/members/$uid",
  validateSearch: validateRangeSearch,
  component: MemberUsageRoute,
});

// ---- project layout ---------------------------------------------------------

// Pages that read project data; on an unusable project they are replaced by an
// edge-state notice (screen 12) instead of firing requests that can only fail.
const DATA_PAGES = new Set(["explorer", "chat", "scans"]);

function ProjectLayout() {
  const { slug } = useParams({ strict: false }) as { slug: string };
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const page = pathname.split("/")[3];
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
    retry: false,
  });
  // `stats: null` on a usable project means the backend refused to open its data
  // (a symlink in the way, `409 unsafe_path`). One cheap data call that opens the
  // project DB tells which problem it is, so Explorer and Chat show the instruction
  // instead of a bare error. The estimate route keeps the initialized gate (the run
  // routes do not), and its key is the one `ScanEstimateCard` reads.
  const usable =
    !!project.data && project.data.initialized && project.data.root_status === "ok" && project.data.source !== "platform";
  const probeWanted = usable && DATA_PAGES.has(page) && project.data?.stats === null;
  const probe = useQuery({
    queryKey: projectKey(slug, "scan-estimate"),
    queryFn: () => projectApi(slug).scanEstimate(),
    enabled: probeWanted,
    retry: false,
  });

  useEffect(() => {
    if (project.isSuccess) setLastProject(slug);
  }, [project.isSuccess, slug]);

  const notFound = project.error instanceof ApiError && project.error.status === 404;
  if (notFound) {
    return (
      <AppShell>
        <NotFoundPage kind="project" />
        <CommandPalette />
      </AppShell>
    );
  }

  const notice = (node: React.ReactNode) => <div className="mx-auto w-full max-w-3xl p-6">{node}</div>;
  const pageBody = (data: ProjectDetails): React.ReactNode => {
    // A production import still cloning (or whose clone failed) has no folder and no
    // project DB yet: its scans and run pages stream the clone; Explorer and Chat wait.
    if (data.importing && DATA_PAGES.has(page)) {
      return page === "scans" ? <Outlet /> : notice(<ImportingNotice project={data} />);
    }
    if (DATA_PAGES.has(page) && data.source === "platform" && page !== "scans") {
      // No local Explorer or Chat for a linked project: the data routes refuse it (M2e).
      return notice(<LinkedElsewhere project={data} />);
    }
    if (page === "chat" && !can(data, "project.chat")) {
      // ChatView never mounts, so no chat request fires for a viewer.
      return notice(
        <Alert data-testid="chat-read-only">
          <AlertTitle>Chat needs the Contributor role</AlertTitle>
          <AlertDescription>
            Ask a project admin for the Contributor role to chat. You can still browse the Explorer and the
            existing rationale cards.
          </AlertDescription>
        </Alert>,
      );
    }
    if (DATA_PAGES.has(page)) {
      // Hold the page back until the probe says its data can be opened.
      if (data.root_status === "ok" && data.initialized && probeWanted && probe.isLoading) return <PageSkeleton />;
      if (data.root_status !== "ok") return notice(<ProjectUnavailable project={data} />);
      if (!data.initialized) return notice(<NotInitialized slug={slug} />);
      if (probe.isError && projectProblem(probe.error).kind === "unsafe_path") {
        return notice(<ProblemAlert problem={projectProblem(probe.error)} />);
      }
    }
    return <Outlet />;
  };
  const body = (
    <QueryState
      query={project}
      loading={<PageSkeleton />}
      errorTitle="Couldn't load this project"
      forbidden={{ what: "this project", grant: "project-admin" }}
      notFound="project"
    >
      {pageBody}
    </QueryState>
  );

  // `key={slug}` remounts the whole subtree on a project switch, so component
  // state (tree expansion, open tab, a half-typed chat draft) cannot carry over.
  return (
    <ProjectProvider key={slug} slug={slug}>
      <AppShell slug={slug} projectName={project.data?.name}>
        {/* A hard-stopped budget on every project page except Chat, which says it in place of its composer. */}
        {project.data?.llm_block === "budget_exceeded" && page !== "chat" && (
          <div className="shrink-0 px-4 pt-3">
            <BudgetNotice scope={project.data.llm_block_scope} />
          </div>
        )}
        {body}
        <CommandPalette slug={slug} />
      </AppShell>
    </ProjectProvider>
  );
}

const projectRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/p/$slug",
  component: ProjectLayout,
});
const projectHomeRoute = createRoute({ getParentRoute: () => projectRoute, path: "/", component: ProjectHome });
const explorerRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "explorer",
  validateSearch: validateExplorerSearch,
  component: ExplorerPage,
});
const chatRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "chat/{-$id}",
  component: ChatView,
});

function ScansRoute() {
  const { runId } = useParams({ strict: false }) as { runId?: string };
  return <ScansPage runId={runId} />;
}
const scansRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "scans/{-$runId}",
  validateSearch: validateScansSearch,
  component: ScansRoute,
});
const projectSettingsRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "settings",
  validateSearch: validateProjectSettingsSearch,
  component: ProjectSettingsPage,
});
const initRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "init",
  // `?step=setup|configure` picks the wizard step (absent, the page chooses from the
  // project's state); `?run=` is the first scan's run. The old `initialize` / `scan`
  // are redirected to `setup` / `configure`, keeping `run`.
  validateSearch: validateInitSearch,
  beforeLoad: ({ params, search, location }) => {
    const raw = parseSearch(location.searchStr ?? "").step;
    if (raw !== undefined && raw !== search.step) {
      throw redirect({ to: "/p/$slug/init", params: { slug: params.slug }, search, replace: true });
    }
  },
  component: InitProjectPage,
});

// ---- legacy root-level links ------------------------------------------------

const legacyExplorerRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/explorer",
  validateSearch: validateExplorerSearch,
  beforeLoad: async ({ context, search }) => {
    const slug = await resolveLastProject(context.queryClient);
    throw redirect(
      slug
        ? { to: "/p/$slug/explorer", params: { slug }, search, replace: true }
        : { to: "/", replace: true },
    );
  },
});

const legacyChatRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/chat/{-$id}",
  beforeLoad: async ({ context, params }) => {
    const slug = await resolveLastProject(context.queryClient);
    throw redirect(
      slug
        ? { to: "/p/$slug/chat/{-$id}", params: { slug, id: params.id }, replace: true }
        : { to: "/", replace: true },
    );
  },
});

// ---- router -----------------------------------------------------------------

const routeTree = rootRoute.addChildren([
  setupRoute,
  baseLayout.addChildren([
    signInRoute,
    githubCallbackRoute,
    githubAppCallbackRoute,
    resetRoute,
    orgsRoute,
    newOrgRoute,
    adminRoute,
    accountRoute,
    connectRoute,
  ]),
  portalLayout.addChildren([
    projectsRoute,
    newProjectRoute,
    linkRoute,
    connectCallbackRoute,
    globalSettingsRoute,
    membersRoute,
    auditRoute,
    usageRoute,
    myUsageRoute,
    memberUsageRoute,
  ]),
  projectRoute.addChildren([
    projectHomeRoute,
    explorerRoute,
    chatRoute,
    scansRoute,
    projectSettingsRoute,
    initRoute,
  ]),
  legacyExplorerRoute,
  legacyChatRoute,
]);

export function createAppRouter(opts: { queryClient: QueryClient; history?: RouterHistory }) {
  return createRouter({
    routeTree,
    context: { queryClient: opts.queryClient },
    history: opts.history,
    parseSearch,
    stringifySearch,
  });
}

export type AppRouter = ReturnType<typeof createAppRouter>;

declare module "@tanstack/react-router" {
  interface Register {
    router: AppRouter;
  }
}
