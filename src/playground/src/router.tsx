import { useEffect } from "react";
import {
  Outlet,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  redirect,
  useParams,
  useRouteContext,
  useRouterState,
  type RouterHistory,
} from "@tanstack/react-router";
import { useQuery, type QueryClient } from "@tanstack/react-query";
import { ApiError, portalApi, portalKey, projectKey, type PortalState } from "./api";
import { getLastProject, setLastProject } from "./lib/lastProject";
import { ProjectProvider } from "./lib/project";
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
import {
  GlobalSettingsPage,
  NotFoundPage,
  ProjectSettingsPage,
  ScansPage,
} from "./pages/placeholders";

// The route tree for §4.9, code-based (a generated `routeTree.gen.ts` would not
// exist yet when `tsc --noEmit` runs ahead of `vite build`).
//
//   /setup                               first run (no shell)
//   /                                    Projects              ┐ portal layout
//   /projects/new                        add-project wizard    │ (sidebar: Projects,
//   /settings                            global settings       ┘  Settings)
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

function DegradedPage({ message }: { message: string }) {
  return (
    <div className="mx-auto max-w-xl p-8">
      <h1 className="text-lg font-semibold">WhyGraph could not start</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        The portal database failed to open or migrate.
      </p>
      <pre className="mt-3 overflow-auto rounded-md bg-muted p-3 text-xs">{message}</pre>
    </div>
  );
}

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

function RootLayout() {
  const { portal } = useRouteContext({ strict: false }) as { portal: PortalState };
  useGlobalShortcuts();
  if (portal.error) return <DegradedPage message={portal.error} />;
  return <Outlet />;
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
    if (!portal.error) {
      const onSetup = location.pathname === "/setup";
      if (!portal.setup_complete && !onSetup) throw redirect({ to: "/setup", replace: true });
      if (portal.setup_complete && onSetup) throw redirect({ to: "/", replace: true });
    }
    return { portal };
  },
  component: RootLayout,
  errorComponent: ({ error, reset }) => <RootError error={error as Error} reset={reset} />,
  notFoundComponent: () => (
    <AppShell>
      <NotFoundPage />
    </AppShell>
  ),
});

const setupRoute = createRoute({ getParentRoute: () => rootRoute, path: "/setup", component: SetupPage });

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
const newProjectRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/projects/new",
  component: AddProjectPage,
});
const globalSettingsRoute = createRoute({
  getParentRoute: () => portalLayout,
  path: "/settings",
  component: GlobalSettingsPage,
});

// ---- project layout ---------------------------------------------------------

// Pages that read project data; on an unusable project they are replaced by a
// notice instead of firing requests that can only fail.
const DATA_PAGES = new Set(["explorer", "chat"]);

function ProjectLayout() {
  const { slug } = useParams({ strict: false }) as { slug: string };
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
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

  const page = pathname.split("/")[3];
  let body: React.ReactNode = <Outlet />;
  if (project.isLoading) {
    body = <p className="p-6 text-sm text-muted-foreground">Loading…</p>;
  } else if (project.isError) {
    body = <p className="p-6 text-sm text-destructive">Failed to load project: {project.error.message}</p>;
  } else if (project.data && DATA_PAGES.has(page)) {
    if (!project.data.initialized) {
      body = <p className="p-6 text-sm text-muted-foreground">This project is not initialized yet.</p>;
    } else if (project.data.root_status !== "ok") {
      body = (
        <p className="p-6 text-sm text-muted-foreground">
          The project folder is not available ({project.data.root_status}).
        </p>
      );
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
  portalLayout.addChildren([projectsRoute, newProjectRoute, globalSettingsRoute]),
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
