import { act, render, screen, waitFor, within } from "@testing-library/react";
import { PROJECT_ACTIONS } from "../lib/permissions";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, useSearch } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, portalKey, projectApi, projectKey } from "../api";
import { buildCrumbs, pageTitle, scanRunKey } from "../components/shell/crumbs";
import { documentTitle } from "../lib/useDocumentTitle";
import { useExplorerSearch, useActiveSessionId } from "../lib/nav";
import { useProjectQuery, useSlug } from "../lib/project";
import { LAST_PROJECT_KEY } from "../lib/lastProject";
import { createAppRouter, parseSearch, stringifySearch } from "../router";
import {
  ORG_SETTINGS_SECTIONS,
  PROJECT_SETTINGS_SECTIONS,
  validateBaseSearch,
  validateInitSearch,
  validateProjectsSearch,
  validateScansSearch,
} from "../lib/routeSearch";
import { useUi } from "../store";
import { configSections } from "../components/portal/ConfigForm";
import { ThemeProvider } from "../theme";

// The heavy views (xyflow, elk, echarts) are replaced by probes that print what
// the router handed them, so these tests cover the route wiring, not the views.
vi.mock("../pages/ExplorerPage", () => ({
  ExplorerPage: function ExplorerProbe() {
    const { node, file } = useExplorerSearch();
    const slug = useSlug();
    const hit = useProjectQuery(["probe"], (api) => api.search("q"));
    return (
      <div data-testid="explorer">
        <span data-testid="slug">{slug}</span>
        <span data-testid="node">{node ?? "-"}</span>
        <span data-testid="file">{file ?? "-"}</span>
        <span data-testid="hit">{hit.data?.results[0]?.name ?? "-"}</span>
      </div>
    );
  },
}));
vi.mock("../pages/UsagePage", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../pages/UsagePage")>()),
  UsagePage: function UsageProbe() {
    const search = useSearch({ strict: false });
    return <div data-testid="usage">{JSON.stringify(search)}</div>;
  },
}));
vi.mock("../components/chat/ChatView", () => ({
  ChatView: function ChatProbe() {
    const id = useActiveSessionId();
    return <div data-testid="chat">{id === null ? "no-session" : `session-${id}`}</div>;
  },
}));

// ---- a fake portal -----------------------------------------------------------

interface Fake {
  setupComplete: boolean;
  slugs: string[];
  /** Serve a production org host (`acme`) instead of the local portal. */
  production?: boolean;
  /** Per-slug overrides of the project details (an importing project, ...). */
  over?: Record<string, Record<string, unknown>>;
  /** The chat sessions every project lists. */
  sessions?: unknown[];
  /** The row `GET .../scans/<id>` answers; without it a run is "not found". */
  run?: unknown;
}

let fake: Fake;
let calls: { url: string; headers: Record<string, string>; method: string }[];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function details(slug: string) {
  return {
    ...baseDetails(slug),
    ...(fake.over?.[slug] ?? {}),
  };
}

function baseDetails(slug: string) {
  return {
    slug,
    name: `Project ${slug}`,
    source: "local",
    root: `/repos/${slug}`,
    remote_url: null,
    initialized: true,
    initialized_at: "2026-01-01T00:00:00Z",
    last_scan_at: null,
    created_at: "2026-01-01T00:00:00Z",
    root_status: "ok",
    running_scan: null,
    restricted: false,
    my_role: "admin",
    permissions: PROJECT_ACTIONS,
    stale: null,
    agents: [],
    missing_key: null,
    mcp_url: `http://127.0.0.1:8765/mcp/${slug}`,
    stats: null,
  };
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  calls.push({
    url: url.pathname + url.search,
    headers: (init?.headers ?? {}) as Record<string, string>,
    method: init?.method ?? "GET",
  });
  const path = url.pathname;
  if (path === "/api/portal/state" && fake.production) {
    return Promise.resolve(
      json({
        mode: "production",
        host_kind: "org",
        base_url: "http://whygraph.localhost:8765",
        setup_complete: true,
        bootstrap_required: false,
        user: { uid: "u1", display_name: "Ada", email: null, role: null, is_instance_admin: false, github_login: "ada" },
        org: { slug: "acme", name: "Acme", role: "owner" },
      }),
    );
  }
  if (path === "/api/github/installations") return Promise.resolve(json({ installations: [] }));
  if (path === "/api/portal/state") {
    return Promise.resolve(
      json({
        mode: "local",
        setup_complete: fake.setupComplete,
        user: fake.setupComplete ? { uid: "u1", display_name: "Tsvetoslav", role: "owner" } : null,
        port: 8765,
        shared_folders: [],
        usage: fake.setupComplete
          ? {
              month: "2026-10",
              resets_at: "2026-11-01T00:00:00+00:00",
              me: null,
              org: { spent_usd: 1, budget_usd: null, pct: null, hard_stop: false, blocked: false },
              projects_over: [],
            }
          : null,
      }),
    );
  }
  if (path === "/api/projects") {
    return Promise.resolve(json({ projects: fake.slugs.map(details) }));
  }
  const m = /^\/api\/projects\/([^/]+)(\/.*)?$/.exec(path);
  if (m) {
    const [, slug, rest] = m;
    if (!fake.slugs.includes(slug)) {
      return Promise.resolve(json({ error: "project not found", code: "not_found" }, 404));
    }
    if (!rest) return Promise.resolve(json(details(slug)));
    if (rest === "/scans") return Promise.resolve(json({ runs: [] }));
    if (/^\/scans\/\d+$/.test(rest)) {
      return Promise.resolve(fake.run ? json(fake.run) : json({ error: "run not found" }, 404));
    }
    if (rest === "/chat/sessions") return Promise.resolve(json(fake.sessions ?? []));
    if (rest === "/search") {
      return Promise.resolve(
        json({ query: "q", results: [{ name: `hit-from-${slug}`, id: slug, analyzed: true }] }),
      );
    }
  }
  return Promise.resolve(json({ error: "unhandled" }, 500));
}

function matchMediaStub() {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })),
  );
}

function mount(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
  });
  const router = createAppRouter({
    queryClient,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return { router, queryClient };
}

const here = (router: ReturnType<typeof mount>["router"]) =>
  router.state.location.pathname + router.state.location.searchStr;

beforeEach(() => {
  fake = { setupComplete: true, slugs: ["alpha", "beta"] };
  calls = [];
  window.localStorage.clear();
  useUi.setState({ paletteOpen: false, navOpen: false });
  matchMediaStub();
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---- search codec ----------------------------------------------------------------

describe("search params", () => {
  it("round-trips qualified names and paths as plain strings", () => {
    const search = {
      node: "pkg.mod.Cls<T>.method:overload",
      file: "src/whygraph/a b/c.py",
    };
    const str = stringifySearch(search);
    expect(str.startsWith("?")).toBe(true);
    expect(parseSearch(str)).toEqual(search);
  });

  it("never JSON-decodes values", () => {
    expect(parseSearch("?node=123&file=true")).toEqual({ node: "123", file: "true" });
    expect(parseSearch("?node=%22quoted%22")).toEqual({ node: '"quoted"' });
  });

  it("drops empty and undefined values and omits a bare '?'", () => {
    expect(stringifySearch({ node: undefined, file: "" })).toBe("");
    expect(stringifySearch({ node: "a", file: undefined })).toBe("?node=a");
  });
});

describe("breadcrumbs", () => {
  const labels = (path: string, ctx?: Parameters<typeof buildCrumbs>[1]) => buildCrumbs(path, ctx).map((c) => c.label);

  it("follows the project route grammar", () => {
    expect(labels("/")).toEqual(["Projects"]);
    expect(labels("/p/alpha", { projectName: "Alpha" })).toEqual(["Projects", "Alpha"]);
    expect(labels("/p/alpha/explorer", { projectName: "Alpha" })).toEqual(["Projects", "Alpha", "Explorer"]);
    expect(labels("/projects/new")).toEqual(["Projects", "Add project"]);
  });

  it("names a run by its title, never by its id", () => {
    expect(labels("/p/alpha/scans/7", { projectName: "Alpha" })).toEqual(["Projects", "Alpha", "Scans", "Scan run"]);
    expect(labels("/p/alpha/scans/7", { projectName: "Alpha", runTitle: "Manual scan - 9 Oct 2026, 14:02" })).toEqual([
      "Projects",
      "Alpha",
      "Scans",
      "Manual scan - 9 Oct 2026, 14:02",
    ]);
    expect(buildCrumbs("/p/alpha/scans/7")[2].to).toEqual({ to: "/p/$slug/scans/{-$runId}", params: { slug: "alpha" } });
  });

  it("carries the chat session's title", () => {
    expect(labels("/p/alpha/chat/3", { projectName: "Alpha", chatTitle: "Why the cache" })).toEqual([
      "Projects",
      "Alpha",
      "Chat",
      "Why the cache",
    ]);
    // "Chat" would start a new chat: plain text, not a link.
    expect(buildCrumbs("/p/alpha/chat/3", { chatTitle: "x" })[2].to).toBeUndefined();
    expect(labels("/p/alpha/chat/3")).toEqual(["Projects", "alpha", "Chat"]);
    expect(labels("/p/alpha/chat")).toEqual(["Projects", "alpha", "Chat"]);
  });

  it("names the wizard step as the page title does", () => {
    expect(labels("/p/alpha/init", { wizardStep: "configure" })).toEqual(["Projects", "Add project", "Configure"]);
    expect(labels("/p/alpha/init", { wizardStep: "setup" })).toEqual(["Projects", "Add project", "Set up"]);
    expect(labels("/p/alpha/init")).toEqual(["Projects", "Add project"]);
  });

  it("names /link, /connect/callback and unknown routes", () => {
    expect(labels("/link")).toEqual(["Projects", "Link a project"]);
    expect(labels("/connect/callback")).toEqual(["Connect"]);
    expect(labels("/no-such-page")).toEqual(["Not found"]);
    expect(labels("/p/alpha/nope", { projectName: "Alpha" })).toEqual(["Projects", "Alpha", "Not found"]);
  });

  it("names the Usage & cost pages", () => {
    expect(labels("/usage")).toEqual(["Usage & cost"]);
    expect(labels("/usage/me")).toEqual(["Usage & cost", "My usage"]);
    // A member's /usage only redirects back to /usage/me: plain text (BUG-19).
    expect(buildCrumbs("/usage/me")[0].to).toBeUndefined();
    expect(buildCrumbs("/usage/me", { orgUsage: true })[0].to).toEqual({ to: "/usage" });
    expect(labels("/usage/members/u7", { memberName: "Ben", orgUsage: true })).toEqual(["Usage & cost", "Ben"]);
    expect(labels("/usage/members/u7")).toEqual(["Usage & cost", "u7"]);
  });

  it("titles a project's own page Overview", () => {
    expect(pageTitle(buildCrumbs("/p/alpha", { projectName: "Alpha" }), "/p/alpha")).toBe("Overview");
    expect(pageTitle(buildCrumbs("/p/alpha/scans"), "/p/alpha/scans")).toBe("Scans");
    expect(documentTitle("Scans", "Alpha")).toBe("Scans · Alpha · WhyGraph");
    expect(documentTitle("Projects")).toBe("Projects · WhyGraph");
    expect(documentTitle("Alpha", "Alpha")).toBe("Alpha · WhyGraph");
  });
});

// ---- routes ----------------------------------------------------------------------

describe("project routes", () => {
  it("reads slug, ?node= and ?file= from a deep link", async () => {
    const node = "pkg.mod.Cls<T>.method";
    const file = "src/pkg/mod.py";
    mount(`/p/alpha/explorer?node=${encodeURIComponent(node)}&file=${encodeURIComponent(file)}`);
    expect(await screen.findByTestId("explorer")).toBeInTheDocument();
    expect(screen.getByTestId("slug")).toHaveTextContent("alpha");
    expect(screen.getByTestId("node")).toHaveTextContent(node);
    expect(screen.getByTestId("file")).toHaveTextContent(file);
  });

  it("ignores ?file= without a node", async () => {
    mount("/p/alpha/explorer?file=src%2Fx.py");
    expect(await screen.findByTestId("explorer")).toBeInTheDocument();
    expect(screen.getByTestId("file")).toHaveTextContent("-");
  });

  it("parses the optional chat session id", async () => {
    mount("/p/alpha/chat/12");
    expect(await screen.findByTestId("chat")).toHaveTextContent("session-12");
  });

  it("opens no session for /chat and for a junk id", async () => {
    mount("/p/alpha/chat/junk");
    expect(await screen.findByTestId("chat")).toHaveTextContent("no-session");
  });

  it("sends X-WhyGraph-Client on every request, GETs included", async () => {
    mount("/p/alpha/explorer");
    await screen.findByTestId("explorer");
    await waitFor(() => expect(screen.getByTestId("hit")).toHaveTextContent("hit-from-alpha"));
    expect(calls.length).toBeGreaterThan(2);
    for (const c of calls) expect(c.headers["X-WhyGraph-Client"]).toBe("1");
  });

  it("remembers the project and shows the project sidebar scope", async () => {
    mount("/p/alpha/explorer");
    await screen.findByTestId("explorer");
    await waitFor(() => expect(window.localStorage.getItem(LAST_PROJECT_KEY)).toBe("alpha"));
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Explorer" })).toHaveAttribute(
      "href",
      "/p/alpha/explorer",
    );
    for (const label of ["Overview", "Scans", "Settings"]) {
      expect(within(nav).getByRole("link", { name: label })).toBeInTheDocument();
    }
    // Chat is the Chats section below the nav, not a nav item (§4.6).
    expect(within(nav).queryByRole("link", { name: "Chat" })).toBeNull();
    expect(within(nav).getByTestId("chats-section")).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Explorer" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(nav).queryByRole("link", { name: "Projects" })).toBeNull();
  });

  it("shows not-found for an unknown slug and does not remember it", async () => {
    mount("/p/ghost/explorer");
    expect(await screen.findByText("Project not found")).toBeInTheDocument();
    expect(screen.queryByTestId("explorer")).toBeNull();
    expect(window.localStorage.getItem(LAST_PROJECT_KEY)).toBeNull();
    // No project data call was ever made for the dead slug.
    expect(calls.some((c) => c.url.startsWith("/api/projects/ghost/search"))).toBe(false);
  });
});

describe("portal routes", () => {
  it("shows the portal sidebar scope on /", async () => {
    mount("/");
    expect(await screen.findByRole("heading", { name: "Projects" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Projects" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
    expect(within(nav).queryByRole("link", { name: "Explorer" })).toBeNull();
    expect(await screen.findByText("Tsvetoslav")).toBeInTheDocument();
  });

  it("serves /usage with its validated search, and a sidebar item", async () => {
    const { router } = mount("/usage?tab=calls&project=alpha&scan_run=7&sort=bogus");
    const probe = await screen.findByTestId("usage");
    expect(JSON.parse(probe.textContent ?? "{}")).toEqual({ tab: "calls", project: "alpha", scan_run: "7" });
    // The router rewrites the address to the validated search.
    await waitFor(() => expect(here(router)).toBe("/usage?tab=calls&project=alpha&scan_run=7"));
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Usage & cost" })).toHaveAttribute("href", "/usage");
  });

  it.each(["/usage/me", "/usage/members/u2"])("keeps the production-only %s not found locally", async (path) => {
    mount(path);
    expect(await screen.findByText("Only on a team portal")).toBeInTheDocument();
    expect(screen.queryByTestId("usage")).toBeNull();
  });

  it.each(["/projects/new", "/settings"])("serves %s", async (path) => {
    const { router } = mount(path);
    await screen.findByRole("navigation", { name: "Main" });
    expect(here(router)).toBe(path);
  });
});

describe("first-run gate", () => {
  it("sends every page to /setup until setup is complete", async () => {
    fake.setupComplete = false;
    const { router } = mount("/p/alpha/explorer");
    expect(await screen.findByText("Welcome to WhyGraph")).toBeInTheDocument();
    expect(here(router)).toBe("/setup");
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
  });

  it("leaves /setup once setup is complete", async () => {
    const { router } = mount("/setup");
    await screen.findByRole("heading", { name: "Projects" });
    expect(here(router)).toBe("/");
  });

  it("renders the portal DB failure instead of the app", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: "migration failed: boom" })),
    );
    mount("/");
    expect(await screen.findByRole("heading", { name: "WhyGraph hit an unexpected error" })).toBeInTheDocument();
    expect(screen.getByTestId("error-details")).toHaveTextContent("migration failed: boom");
  });
});

describe("legacy links", () => {
  it("redirects /explorer?node= to the last-used project, keeping the search", async () => {
    window.localStorage.setItem(LAST_PROJECT_KEY, "beta");
    const { router } = mount("/explorer?node=a.b&file=src%2Fx.py");
    expect(await screen.findByTestId("explorer")).toBeInTheDocument();
    expect(here(router)).toBe("/p/beta/explorer?node=a.b&file=src%2Fx.py");
    expect(screen.getByTestId("slug")).toHaveTextContent("beta");
    expect(screen.getByTestId("node")).toHaveTextContent("a.b");
  });

  it("redirects /chat/<id> to the last-used project", async () => {
    window.localStorage.setItem(LAST_PROJECT_KEY, "alpha");
    const { router } = mount("/chat/4");
    expect(await screen.findByTestId("chat")).toHaveTextContent("session-4");
    expect(here(router)).toBe("/p/alpha/chat/4");
  });

  it("redirects a bare /chat", async () => {
    window.localStorage.setItem(LAST_PROJECT_KEY, "alpha");
    const { router } = mount("/chat");
    expect(await screen.findByTestId("chat")).toHaveTextContent("no-session");
    expect(here(router)).toBe("/p/alpha/chat");
  });

  it("falls back to the projects page when lastProject is stale", async () => {
    window.localStorage.setItem(LAST_PROJECT_KEY, "removed-project");
    const { router } = mount("/explorer?node=a.b");
    await screen.findByRole("heading", { name: "Projects" });
    expect(here(router)).toBe("/");
    expect(screen.queryByTestId("explorer")).toBeNull();
  });

  it("falls back to the projects page when nothing was remembered", async () => {
    const { router } = mount("/chat/4");
    await screen.findByRole("heading", { name: "Projects" });
    expect(here(router)).toBe("/");
  });

  it("survives blocked storage", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const { router } = mount("/explorer");
    await screen.findByRole("heading", { name: "Projects" });
    expect(here(router)).toBe("/");
    vi.restoreAllMocks();
  });
});

describe("switching project", () => {
  it("keeps no selection, no old-slug query and no old-slug data", async () => {
    const { router, queryClient } = mount("/p/alpha/explorer?node=a.b&file=src%2Fa.py");
    await waitFor(() => expect(screen.getByTestId("hit")).toHaveTextContent("hit-from-alpha"));

    await act(async () => {
      await router.navigate({ to: "/p/$slug/explorer", params: { slug: "beta" } });
    });

    await waitFor(() => expect(screen.getByTestId("slug")).toHaveTextContent("beta"));
    // The selection lived in the URL, so it is gone with it.
    expect(here(router)).toBe("/p/beta/explorer");
    expect(screen.getByTestId("node")).toHaveTextContent("-");
    await waitFor(() => expect(screen.getByTestId("hit")).toHaveTextContent("hit-from-beta"));

    // After the switch, nothing is asked of the old project.
    const switchAt = calls.findIndex((c) => c.url.startsWith("/api/projects/beta"));
    expect(switchAt).toBeGreaterThan(-1);
    expect(calls.slice(switchAt).some((c) => c.url.startsWith("/api/projects/alpha"))).toBe(false);

    // Two projects never share a cache entry: every project key is slug-prefixed.
    const keys = queryClient
      .getQueryCache()
      .getAll()
      .map((q) => q.queryKey);
    expect(keys).toContainEqual(projectKey("alpha", "probe"));
    expect(keys).toContainEqual(projectKey("beta", "probe"));
    for (const k of keys) expect(["alpha", "beta", "@portal"]).toContain(k[0]);
  });

  it("switches from the sidebar project switcher", async () => {
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/explorer");
    await screen.findByTestId("explorer");
    await user.click(screen.getByRole("button", { name: "Switch project" }));
    await user.click(await screen.findByRole("menuitem", { name: /Project beta/ }));
    await waitFor(() => expect(here(router)).toBe("/p/beta"));
    expect(window.localStorage.getItem(LAST_PROJECT_KEY)).toBe("beta");
  });
});

describe("keyboard", () => {
  it("opens the command menu with Cmd-K on any page", async () => {
    mount("/");
    await screen.findByRole("heading", { name: "Projects" });
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }));
    });
    expect(await screen.findByPlaceholderText(/Go to a page or project/)).toBeInTheDocument();
  });

  it("jumps with g then a letter, but not while typing", async () => {
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/explorer");
    await screen.findByTestId("explorer");
    await user.keyboard("gp");
    await waitFor(() => expect(here(router)).toBe("/"));

    await act(async () => {
      await router.navigate({ to: "/p/$slug", params: { slug: "alpha" } });
    });
    await user.keyboard("ge");
    await waitFor(() => expect(here(router)).toBe("/p/alpha/explorer"));
  });
});

// ---- M2f-3 route plumbing (S12) -----------------------------------------------------

describe("local /account (BUG-10)", () => {
  it("goes to the portal's settings: local mode has no account", async () => {
    const { router } = mount("/account");
    await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() => expect(here(router)).toBe("/settings"));
    expect(screen.queryByText("Organizations")).toBeNull();
  });
});

describe("wizard steps", () => {
  it("validates step and run, mapping the old step names", () => {
    expect(validateInitSearch({ step: "setup", run: "12" })).toEqual({ step: "setup", run: 12 });
    expect(validateInitSearch({ step: "initialize" })).toEqual({ step: "setup", run: undefined });
    expect(validateInitSearch({ step: "scan", run: 5 })).toEqual({ step: "configure", run: 5 });
    expect(validateInitSearch({ step: "bogus", run: "-1" })).toEqual({ step: undefined, run: undefined });
    expect(validateInitSearch({ run: "0" })).toEqual({ step: undefined, run: undefined });
  });

  it.each([
    ["/p/alpha/init?step=initialize", "/p/alpha/init?step=setup"],
    ["/p/alpha/init?step=scan&run=7", "/p/alpha/init?step=configure&run=7"],
    ["/p/alpha/init?step=configure&run=x", "/p/alpha/init?step=configure"],
  ])("redirects %s to %s", async (from, to) => {
    const { router } = mount(from);
    await waitFor(() => expect(here(router)).toBe(to));
  });
});

describe("search params for the M2f-3 pages", () => {
  it("keeps the Projects list's q and a known sort", async () => {
    expect(validateProjectsSearch({ q: "api", sort: "status" })).toEqual({ q: "api", sort: "status" });
    expect(validateProjectsSearch({ q: "", sort: "bogus" })).toEqual({ q: undefined, sort: undefined });
    const { router } = mount("/?q=api&sort=bogus");
    await screen.findByRole("heading", { name: "Projects" });
    await waitFor(() => expect(here(router)).toBe("/?q=api"));
  });

  it("keeps the scan history's filters, dropping unknown values", async () => {
    expect(
      validateScansSearch({ status: "ok,bogus,failed,ok", trigger: ["manual", "nope"], type: "full", requester: "system" }),
    ).toEqual({ status: ["ok", "failed"], trigger: ["manual"], type: "full", requester: "system" });
    expect(validateScansSearch({ status: "", type: "kind", requester: "a b" })).toEqual({
      status: undefined,
      trigger: undefined,
      type: undefined,
      requester: undefined,
    });
    const { router } = mount("/p/alpha/scans?status=ok%2Cbogus&trigger=hook&type=sync&requester=u7");
    await screen.findByRole("heading", { name: "Scans" });
    await waitFor(() => expect(here(router)).toBe("/p/alpha/scans?status=ok&trigger=hook&type=sync&requester=u7"));
    const kept = mount("/p/alpha/scans?type=bogus&requester=u7");
    await waitFor(() => expect(here(kept.router)).toBe("/p/alpha/scans?requester=u7"));
    // A list round-trips through the address as one comma-joined value.
    expect(stringifySearch({ status: ["ok", "failed"] })).toBe("?status=ok%2Cfailed");
    expect(validateScansSearch(parseSearch("?status=ok%2Cfailed")).status).toEqual(["ok", "failed"]);
  });

  it("keeps a settings section the page has", async () => {
    const project = mount("/p/alpha/settings?section=danger");
    await waitFor(() => expect(here(project.router)).toBe("/p/alpha/settings?section=danger"));
  });

  it("drops a section global settings does not have", async () => {
    const global = mount("/settings?section=agents");
    await waitFor(() => expect(here(global.router)).toBe("/settings"));
  });

  it("deep-links every section a settings page renders (SET-1)", async () => {
    const hooks = mount("/p/alpha/settings?section=hooks");
    await waitFor(() => expect(here(hooks.router)).toBe("/p/alpha/settings?section=hooks"));
    const projectIds = [
      ...configSections("project", { production: false, source: "local" }),
      ...configSections("project", { production: true, source: "github" }),
    ].map((s) => s.id);
    for (const id of [...projectIds, "general", "budgets", "agents", "access", "connections", "danger"]) {
      expect(PROJECT_SETTINGS_SECTIONS as readonly string[]).toContain(id);
    }
    const orgIds = [
      ...configSections("global", { production: false }),
      ...configSections("global", { production: true, configurer: true }),
    ].map((s) => s.id);
    for (const id of [...orgIds, "general", "budgets", "danger"]) {
      expect(ORG_SETTINGS_SECTIONS as readonly string[]).toContain(id);
    }
  });

  it("validates the base routes' next, stay and from", () => {
    expect(validateBaseSearch({ next: "/x", stay: "1", from: "acme" })).toEqual({ next: "/x", stay: true, from: "acme" });
    expect(validateBaseSearch({ stay: "0", from: "Not A Slug" })).toEqual({
      next: undefined,
      stay: undefined,
      from: undefined,
    });
    // `stay` is written back as `?stay=1`, never `?stay=true`.
    expect(stringifySearch({ stay: true, from: "acme" })).toBe("?stay=1&from=acme");
    expect(stringifySearch({ stay: false })).toBe("");
  });
});

describe("an importing project", () => {
  const importing = {
    source: "github",
    root: null,
    importing: true,
    initialized: false,
    initialized_at: null,
    root_status: "missing",
    github_full_name: "acme/alpha",
    running_scan: { id: 5, status: "running", trigger: "initial" },
  };

  it.each(["explorer", "chat"])("shows the importing notice on %s instead of the page", async (page) => {
    fake.over = { alpha: importing };
    mount(`/p/alpha/${page}`);
    const notice = await screen.findByTestId("importing-notice");
    expect(notice).toHaveTextContent("Importing acme/alpha");
    expect(within(notice).getByRole("link", { name: "Follow the import" })).toHaveAttribute("href", "/p/alpha/scans/5");
    expect(screen.queryByTestId(page)).toBeNull();
    expect(screen.queryByTestId("project-unavailable")).toBeNull();
    expect(screen.queryByTestId("not-initialized")).toBeNull();
  });

  it("says the import did not finish when nothing is running", async () => {
    fake.over = { alpha: { ...importing, running_scan: null } };
    mount("/p/alpha/explorer");
    const notice = await screen.findByTestId("importing-notice");
    expect(notice).toHaveTextContent("The import did not finish");
    expect(within(notice).getByRole("link", { name: "Open scans" })).toHaveAttribute("href", "/p/alpha/scans");
  });

  it("is not a missing folder on the Overview", async () => {
    fake.over = { alpha: importing };
    mount("/p/alpha/");
    expect(await screen.findByTestId("importing-notice")).toBeInTheDocument();
    expect(screen.queryByTestId("project-unavailable")).toBeNull();
    expect(screen.queryByTestId("not-initialized")).toBeNull();
  });

  it("renders the scans history and the run page", async () => {
    fake.over = { alpha: importing };
    mount("/p/alpha/scans");
    expect(await screen.findByRole("heading", { name: "Scans" })).toBeInTheDocument();
    expect(screen.queryByTestId("project-unavailable")).toBeNull();
    expect(screen.queryByTestId("not-initialized")).toBeNull();
    expect(screen.queryByTestId("importing-notice")).toBeNull();
  });

  it("renders the run page", async () => {
    fake.over = { alpha: importing };
    fake.run = {
      id: 5,
      kind: "sync",
      trigger: "initial",
      analyze: false,
      status: "queued",
      queued_at: "2026-10-09T14:02:00Z",
      requested_by: null,
      started_at: null,
      finished_at: null,
      summary: null,
    };
    mount("/p/alpha/scans/5");
    expect(await screen.findByTestId("run-view")).toHaveTextContent("Import - ");
    expect(screen.queryByTestId("project-unavailable")).toBeNull();
    expect(screen.queryByTestId("not-initialized")).toBeNull();
  });
});

describe("the GitHub installations query (BUG-23)", () => {
  const installationCalls = () => calls.filter((c) => c.url.startsWith("/api/github/installations")).length;

  it("is never asked for by the project Overview, also after the import page", async () => {
    fake.production = true;
    const { router } = mount("/p/alpha/");
    await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() => expect(calls.some((c) => c.url === "/api/projects/alpha")).toBe(true));
    expect(installationCalls()).toBe(0);

    await act(async () => {
      await router.navigate({ to: "/projects/new" });
    });
    await waitFor(() => expect(installationCalls()).toBe(1));

    await act(async () => {
      await router.navigate({ to: "/p/$slug", params: { slug: "alpha" } });
    });
    await waitFor(() => expect(here(router)).toBe("/p/alpha"));
    // Let any late query settle, then check nothing new was asked.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(installationCalls()).toBe(1);
  });
});

// ---- the API client --------------------------------------------------------------

describe("projectApi", () => {
  it("targets /api/projects/<slug> and encodes the slug", async () => {
    const f = vi.fn(async () => json({ entries: [] }));
    vi.stubGlobal("fetch", f);
    await projectApi("my-repo").tree({ dir: "src" });
    expect(f).toHaveBeenCalledWith(
      "/api/projects/my-repo/tree?dir=src",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("sends the client header on writes and on the chat stream", async () => {
    const f = vi.fn(async (_url: string, _init?: RequestInit) =>
      _url.endsWith("/messages")
        ? new Response(new ReadableStream({ start: (c) => c.close() }), { status: 200 })
        : json({ id: 1 }, 201),
    );
    vi.stubGlobal("fetch", f);
    const api = projectApi("alpha");
    await api.chatCreateSession({});
    await api.streamChat(1, "hi", () => {});
    await api.chatDeleteSession(1).catch(() => {});
    for (const [, init] of f.mock.calls) {
      expect((init?.headers as Record<string, string>)["X-WhyGraph-Client"]).toBe("1");
    }
    const urls = f.mock.calls.map(([u]) => u);
    expect(urls).toContain("/api/projects/alpha/chat/sessions");
    expect(urls).toContain("/api/projects/alpha/chat/sessions/1/messages");
  });

  it("surfaces the portal's error body and code", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "nope", code: "not_found" }, 404)));
    await expect(projectApi("x").node("a.b")).rejects.toMatchObject({
      status: 404,
      message: "nope",
      code: "not_found",
    });
    expect(new ApiError(1, "m")).toBeInstanceOf(Error);
  });

  it("namespaces portal keys away from slugs", () => {
    expect(portalKey("state")[0]).toBe("@portal");
    expect(projectKey("a", "tree")).toEqual(["a", "tree"]);
  });
});

// ---- the shell: keyboard, titles, the project switcher (S14) --------------------------

describe("shell keyboard and titles", () => {
  it("starts with a skip link that moves focus to the main region", async () => {
    const user = userEvent.setup();
    mount("/");
    await screen.findByRole("heading", { name: "Projects" });
    const skip = screen.getByRole("link", { name: "Skip to content" });
    await user.tab();
    expect(skip).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(document.getElementById("main")).toHaveFocus();
  });

  it("moves focus to the new page's h1 and announces it on a path change", async () => {
    const { router } = mount("/p/alpha/explorer");
    await screen.findByTestId("explorer");
    await act(async () => {
      await router.navigate({ to: "/" });
    });
    const heading = await screen.findByRole("heading", { level: 1, name: "Projects" });
    await waitFor(() => expect(heading).toHaveFocus());
    // A heading is not a control: no focus box around the page title.
    expect(heading).toHaveAttribute("tabindex", "-1");
    expect(heading.style.outline).toMatch(/none/);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Projects"));
  });

  it("falls back to the main region on a page without an h1, and ignores search-only changes", async () => {
    const { router } = mount("/");
    await screen.findByRole("heading", { name: "Projects" });
    await act(async () => {
      await router.navigate({ to: "/p/$slug/explorer", params: { slug: "alpha" } });
    });
    await screen.findByTestId("explorer");
    await waitFor(() => expect(document.getElementById("main")).toHaveFocus(), { timeout: 2000 });

    // A selection (search only) leaves focus where the person put it.
    const button = screen.getByRole("button", { name: "Switch project" });
    button.focus();
    await act(async () => {
      await router.navigate({ to: "/p/$slug/explorer", params: { slug: "alpha" }, search: { node: "a.b" } });
    });
    await waitFor(() => expect(screen.getByTestId("node")).toHaveTextContent("a.b"));
    await new Promise((r) => setTimeout(r, 700));
    expect(button).toHaveFocus();
  });

  it("titles the document '<page> · <project> · WhyGraph'", async () => {
    const { router } = mount("/p/alpha/explorer");
    await screen.findByTestId("explorer");
    await waitFor(() => expect(document.title).toBe("Explorer · Project alpha · WhyGraph"));
    await act(async () => {
      await router.navigate({ to: "/" });
    });
    await waitFor(() => expect(document.title).toBe("Projects · WhyGraph"));
  });

  it("names the open chat session and the open run in the crumbs, from the query cache", async () => {
    fake.sessions = [
      { id: 12, title: "Why the cache", provider: "openai", model: "m", created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" },
    ];
    mount("/p/alpha/chat/12");
    const crumbs = await screen.findByRole("navigation", { name: "breadcrumb" });
    await waitFor(() => expect(crumbs).toHaveTextContent("Why the cache"));
    // One line in the h-12 header: no wrap, the middle crumbs truncate first and the
    // last (the page) keeps most of the line.
    const list = within(crumbs).getByTestId("breadcrumbs");
    expect(list.className).toContain("flex-nowrap");
    expect(list.className).not.toMatch(/(^|\s)flex-wrap(\s|$)/);
    const items = list.querySelectorAll('[data-slot="breadcrumb-item"]');
    const last = items[items.length - 1];
    expect(last.className).toContain("shrink-0");
    expect(last.className).toContain("max-w-[70%]");
    expect(last.querySelector('[data-slot="breadcrumb-page"]')?.className).toContain("truncate");
    for (const middle of Array.from(items).slice(0, -1)) expect(middle.className).toContain("min-w-0");
    // R1: on a phone the middle crumbs collapse into one "..." item; the first and last stay.
    const collapsed = within(list).getByTestId("crumbs-collapsed");
    expect(collapsed).toHaveTextContent("...");
    expect(collapsed.className).toContain("sm:hidden");
    expect(items[0].className).not.toContain("max-sm:hidden");
    expect(items[items.length - 1].className).not.toContain("max-sm:hidden");
    expect(Array.from(items).filter((i) => i.className.includes("max-sm:hidden")).length).toBeGreaterThan(0);
    await waitFor(() => expect(document.title).toBe("Why the cache · Project alpha · WhyGraph"));
    document.body.innerHTML = "";

    const { queryClient } = mount("/p/alpha/scans/5");
    const runCrumbs = await screen.findByRole("navigation", { name: "breadcrumb" });
    expect(runCrumbs).toHaveTextContent("Scan run");
    expect(runCrumbs).not.toHaveTextContent("#5");
    act(() => {
      queryClient.setQueryData(scanRunKey("alpha", 5), {
        id: 5,
        kind: "scan",
        trigger: "manual",
        analyze: true,
        status: "ok",
        queued_at: "2026-10-09T14:02:00Z",
        requested_by: null,
        started_at: null,
        finished_at: null,
        summary: null,
      });
    });
    await waitFor(() => expect(runCrumbs).toHaveTextContent(/Full rescan - /));
  });

  it("names an unknown route 'Not found' in the crumbs and the page", async () => {
    mount("/no-such-page");
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    const crumbs = screen.getByRole("navigation", { name: "breadcrumb" });
    expect(crumbs).toHaveTextContent("Not found");
    expect(crumbs).not.toHaveTextContent("Projects");
  });

  it("shows a static Local label instead of the org switcher in local mode", async () => {
    mount("/");
    expect(await screen.findByTestId("org-label")).toHaveTextContent("Local");
    expect(screen.queryByTestId("org-switcher")).toBeNull();
  });
});

describe("project switcher (NAV-5)", () => {
  it("lists All projects once at the top, every project with its status, and Add project", async () => {
    const user = userEvent.setup();
    mount("/p/alpha/explorer");
    await screen.findByTestId("explorer");
    await user.click(screen.getByRole("button", { name: "Switch project" }));
    const items = await screen.findAllByRole("menuitem");
    expect(items[0]).toHaveTextContent("All projects");
    expect(items.filter((i) => i.textContent?.includes("All projects"))).toHaveLength(1);
    const alpha = screen.getByRole("menuitem", { name: /Project alpha/ });
    expect(alpha).toHaveAttribute("aria-current", "true");
    expect(alpha).toHaveTextContent("Not scanned yet");
    expect(screen.getByRole("menuitem", { name: /Project beta/ })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("menuitem", { name: "Add project" })).toBeInTheDocument();
    expect(screen.queryByText("•")).toBeNull();
  });
});
