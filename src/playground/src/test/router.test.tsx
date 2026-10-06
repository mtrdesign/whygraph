import { act, render, screen, waitFor, within } from "@testing-library/react";
import { PROJECT_ACTIONS } from "../lib/permissions";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, portalKey, projectApi, projectKey } from "../api";
import { buildCrumbs } from "../components/shell/crumbs";
import { useExplorerSearch, useActiveSessionId } from "../lib/nav";
import { useProjectQuery, useSlug } from "../lib/project";
import { LAST_PROJECT_KEY } from "../lib/lastProject";
import { createAppRouter, parseSearch, stringifySearch } from "../router";
import { useUi } from "../store";
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
  if (path === "/api/portal/state") {
    return Promise.resolve(
      json({
        mode: "local",
        setup_complete: fake.setupComplete,
        user: fake.setupComplete ? { uid: "u1", display_name: "Tsvetoslav", role: "owner" } : null,
        port: 8765,
        shared_folders: [],
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
  it("follows the project route grammar", () => {
    expect(buildCrumbs("/", undefined).map((c) => c.label)).toEqual(["Projects"]);
    expect(buildCrumbs("/p/alpha", "Alpha").map((c) => c.label)).toEqual(["Projects", "Alpha"]);
    expect(buildCrumbs("/p/alpha/explorer", "Alpha").map((c) => c.label)).toEqual([
      "Projects",
      "Alpha",
      "Explorer",
    ]);
    expect(buildCrumbs("/p/alpha/scans/7", "Alpha").map((c) => c.label)).toEqual([
      "Projects",
      "Alpha",
      "Scans",
      "Run #7",
    ]);
    expect(buildCrumbs("/projects/new").map((c) => c.label)).toEqual(["Projects", "Add project"]);
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
    for (const label of ["Overview", "Chat", "Scans", "Settings"]) {
      expect(within(nav).getByRole("link", { name: label })).toBeInTheDocument();
    }
    expect(within(nav).getByRole("link", { name: "Explorer" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(nav).queryByRole("link", { name: "Projects" })).toBeNull();
  });

  it("shows not-found for an unknown slug and does not remember it", async () => {
    mount("/p/ghost/explorer");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
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
    expect(await screen.findByText("WhyGraph could not start")).toBeInTheDocument();
    expect(screen.getByText(/migration failed: boom/)).toBeInTheDocument();
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
