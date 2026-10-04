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
import { ApiError, portalApi, portalKey, projectApi, projectKey, setBaseUrl, type PortalState } from "./api";
import { projectProblem } from "./lib/errors";
import { getLastProject, setLastProject } from "./lib/lastProject";
import { ProjectProvider } from "./lib/project";
import { canAdmin, isProduction, isSafeNext, signInUrl, usePortalState } from "./lib/identity";
import { hardNavigate } from "./lib/navigation";
import { AppShell } from "./components/shell/AppShell";
import { useGlobalShortcuts } from "./components/shell/shortcuts";
import { CommandPalette } from "./components/CommandPalette";
import { ChatView } from "./components/chat/ChatView";
import { ExplorerPage } from "./pages/ExplorerPage";
import { ProjectHome } from "./pages/ProjectHome";
import { ProjectsPage } from "./pages/ProjectsPage";
import { AddProjectPage } from "./pages/AddProjectPage";
import { InitProjectPage, type InitStep } from "./pages/InitProjectPage";
import { SetupPage } from "./pages/SetupPage";
import { GlobalSettingsPage } from "./pages/GlobalSettingsPage";
import { MembersPage } from "./pages/MembersPage";
import { ProjectSettingsPage } from "./pages/ProjectSettingsPage";
import { ScansPage } from "./pages/ScansPage";
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
  DegradedPage,
  NotFoundPage,
  NotInitialized,
  ProblemAlert,
  ProjectUnavailable,
} from "./components/portal/EdgeStates";

// The route tree for §4.9, code-based (a generated `routeTree.gen.ts` would not
// exist yet when `tsc --noEmit` runs ahead of `vite build`).
//
//   /setup                               first run (no shell); production: the bootstrap
//   /signin /auth/github /auth/github-app /reset /orgs /orgs/new /admin /account
//                                        production base host only (no AppShell)
//   /                                    Projects              ┐ portal layout
//   /projects/new                        add-project wizard    │ (sidebar: Projects,
//   /settings                            global settings       │  Settings, and
//   /members                             org members           ┘  Members in production)
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
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
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

function RootError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="mx-auto max-w-xl p-8">
      <h1 className="text-lg font-semibold">Something went wrong</h1>
      <pre className="mt-3 overflow-auto rounded-md bg-muted p-3 text-xs">{error.message}</pre>
      <button
        type="button"
        onClick={reset}
        className="mt-3 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
      >
        Try again
      </button>
    </div>
  );
}

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
]);
const SIGNED_OUT_PATHS = new Set(["/signin", "/auth/github", "/reset"]);

/**
 * Where the base host sends this request instead of rendering it, or `null` to
 * render. `/signin` with a valid `next` while signed in is *not* redirected: the
 * route renders the "session not received" page there, which breaks the loop
 * org host (no cookie) -> sign-in -> picker -> org host.
 */
export function baseHostRedirect(portal: PortalState, pathname: string): string | null {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  if (portal.bootstrap_required) return path === "/setup" ? null : "/setup";
  if (path === "/setup") return "/";
  if (!portal.user) return SIGNED_OUT_PATHS.has(path) ? null : "/signin";
  if (path === "/") return "/orgs";
  if (!BASE_PATHS.has(path)) return "/";
  if (path === "/admin" && !portal.user.is_instance_admin) return "/orgs";
  return null;
}

function RootLayout() {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  useGlobalShortcuts();
  if (portal.error) return <DegradedPage message={portal.error} />;
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
    setBaseUrl(!portal.error && kind && kind !== "local" ? (portal.base_url ?? null) : null);
    if (!portal.error && kind === "base") {
      const to = baseHostRedirect(portal, location.pathname);
      if (to) throw redirect({ to, replace: true });
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
      const onSetup = location.pathname === "/setup";
      if (!portal.setup_complete && !onSetup) throw redirect({ to: "/setup", replace: true });
      if (portal.setup_complete && onSetup) throw redirect({ to: "/", replace: true });
    }
    return { portal };
  },
  component: RootLayout,
  errorComponent: ({ error, reset }) => <RootError error={error as Error} reset={reset} />,
  notFoundComponent: RootNotFound,
});

// One /setup route: local mode's first-run page, or production's bootstrap.
function SetupRoute() {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  return portal.mode === "production" ? <BootstrapPage /> : <SetupPage />;
}
const setupRoute = createRoute({ getParentRoute: () => rootRoute, path: "/setup", component: SetupRoute });

// ---- base-host pages (production; no AppShell) ----------------------------------

const validateNext = (search: Record<string, unknown>): { next?: string } => ({ next: text(search.next) });

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
const accountRoute = createRoute({ getParentRoute: () => baseLayout, path: "/account", component: AccountPage });

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
const projectsRoute = createRoute({ getParentRoute: () => portalLayout, path: "/", component: ProjectsPage });
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
  component: NewProjectRoute,
});
const globalSettingsRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/settings",
  component: GlobalSettingsPage,
});

// Org members exist only in production; local mode has one implicit user.
function MembersRoute() {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  return isProduction(portal) ? <MembersPage /> : <NotFoundPage />;
}
const membersRoute = createRoute({ getParentRoute: () => portalLayout, path: "/members", component: MembersRoute });

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
  // (a symlink in the way, `409 unsafe_path`). One cheap data call tells which
  // problem it is, so Explorer and Chat show the instruction instead of a bare error.
  const usable = !!project.data && project.data.initialized && project.data.root_status === "ok";
  const probeWanted = usable && DATA_PAGES.has(page) && project.data?.stats === null;
  const probe = useQuery({
    queryKey: projectKey(slug, "scans"),
    queryFn: () => projectApi(slug).scans(),
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
        <NotFoundPage />
        <CommandPalette />
      </AppShell>
    );
  }

  const notice = (node: React.ReactNode) => <div className="mx-auto w-full max-w-3xl p-6">{node}</div>;
  let body: React.ReactNode = <Outlet />;
  if (project.isLoading) {
    body = <p className="p-6 text-sm text-muted-foreground">Loading…</p>;
  } else if (project.isError) {
    body = <p className="p-6 text-sm text-destructive">Failed to load project: {project.error.message}</p>;
  } else if (project.data && DATA_PAGES.has(page)) {
    if (project.data.root_status === "ok" && project.data.initialized && probeWanted && probe.isLoading) {
      // Hold the page back until the probe says its data can be opened.
      body = <p className="p-6 text-sm text-muted-foreground">Loading…</p>;
    } else if (project.data.root_status !== "ok") {
      body = notice(<ProjectUnavailable project={project.data} />);
    } else if (!project.data.initialized) {
      body = notice(<NotInitialized slug={slug} />);
    } else if (probe.isError && projectProblem(probe.error).kind === "unsafe_path") {
      body = notice(<ProblemAlert problem={projectProblem(probe.error)} />);
    }
  }

  // `key={slug}` remounts the whole subtree on a project switch, so component
  // state (tree expansion, open tab, a half-typed chat draft) cannot carry over.
  return (
    <ProjectProvider key={slug} slug={slug}>
      <AppShell slug={slug} projectName={project.data?.name}>
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
  component: ScansRoute,
});
const projectSettingsRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "settings",
  component: ProjectSettingsPage,
});
const INIT_STEPS: readonly InitStep[] = ["configure", "initialize", "scan"];

const initRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "init",
  // `?step=` picks the wizard step; absent, the page chooses from the project's state.
  validateSearch: (search: Record<string, unknown>): { step?: InitStep } => ({
    step: INIT_STEPS.find((s) => s === search.step),
  }),
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
  ]),
  portalLayout.addChildren([projectsRoute, newProjectRoute, globalSettingsRoute, membersRoute]),
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
