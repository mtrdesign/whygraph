import { render, screen, waitFor, within } from "@testing-library/react";
import { PROJECT_ACTIONS } from "../lib/permissions";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LinkStatus, ProjectLink } from "../api";
import { linkError } from "../lib/errors";
import { hardNavigate } from "../lib/navigation";
import { accountUrl, linkNotice, safeHref } from "../lib/platformLink";
import { parseLinkPlatform } from "../pages/LinkPage";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";
import { ApiError } from "../api";

// The local portal's half of M2e: the /link and /connect/callback pages, the
// platform source of the wizard, and how a linked project shows up.

type Json = Record<string, unknown>;
const hard = vi.mocked(hardNavigate);
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const AUTHORIZE = "https://whygraph.example.com/connect?state=SERVERSTATE&via=server";

function link(over: Partial<ProjectLink> = {}): ProjectLink {
  return {
    platform_origin: "https://whygraph.example.com",
    org: "acme",
    remote_slug: "alpha",
    status: "ok",
    status_reason: null,
    last_platform_head: "abc123",
    project_role: "contributor",
    explorer_url: "https://acme.whygraph.example.com/p/alpha/explorer",
    chat_url: "https://acme.whygraph.example.com/p/alpha/chat",
    manage_url: "https://acme.whygraph.example.com/p/alpha/settings",
    ...over,
  };
}

function project(over: Json = {}): Json {
  return {
    slug: "alpha",
    name: "Alpha",
    source: "platform",
    root: "/repos/alpha",
    remote_url: null,
    initialized: true,
    initialized_at: "2026-01-01T00:00:00+00:00",
    last_scan_at: "2026-01-02T00:00:00+00:00",
    created_at: "2026-01-01T00:00:00+00:00",
    root_status: "ok",
    running_scan: null,
    restricted: false,
    my_role: "admin",
    permissions: PROJECT_ACTIONS,
    stale: null,
    source_supported: true,
    access_lost: false,
    access_lost_reason: null,
    github_full_name: null,
    installation_account: null,
    link: link(),
    agents: [],
    missing_key: null,
    mcp_url: "http://127.0.0.1:8765/mcp/alpha",
    stats: null,
    ...over,
  };
}

interface Fake {
  calls: { method: string; path: string; body: Json | null }[];
  projects: Json[];
  routes: Record<string, (body: Json | null) => Response>;
  config: Json;
}
let fake: Fake;

const PENDING = {
  link_id: "L1",
  platform_origin: "https://whygraph.example.com",
  org: "acme",
  project: { slug: "alpha", name: "Alpha" },
  clone_url: "https://github.com/acme/alpha.git",
  clone_command: "git clone https://github.com/acme/alpha.git",
  slug_taken: false,
  reconnect: null,
  candidates: [{ path: "/repos/alpha", name: "alpha", match: "origin" }],
  other_repos: [{ path: "/repos/other", name: "other" }],
};

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const path = url.pathname;
  const method = init?.method ?? "GET";
  const body = init?.body ? (JSON.parse(String(init.body)) as Json) : null;
  fake.calls.push({ method, path, body });
  const route = fake.routes[`${method} ${path}`];
  if (route) return Promise.resolve(route(body));
  if (path === "/api/portal/state")
    return Promise.resolve(
      json({
        mode: "local",
        setup_complete: true,
        user: { uid: "u1", display_name: "Ada", role: "owner" },
        port: 8765,
        shared_folders: ["/repos"],
        hostname: "ada-laptop",
        port_change: null,
      }),
    );
  if (path === "/api/projects" && method === "GET") return Promise.resolve(json({ projects: fake.projects }));
  const m = /^\/api\/projects\/([^/]+)(\/.*)?$/.exec(path);
  if (m) {
    const proj = fake.projects.find((p) => p.slug === m[1]);
    if (!proj) return Promise.resolve(json({ error: "nf", code: "not_found" }, 404));
    if (!m[2]) return Promise.resolve(json(proj));
    if (m[2] === "/config" && method === "GET")
      return Promise.resolve(
        json({
          config: fake.config,
          secrets: { llm: {}, github_token: { set: false, hint: null } },
          import: { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] },
        }),
      );
    if (m[2] === "/config" && method === "PUT") return Promise.resolve(json({ config: body?.config, secrets: {}, import: {} }));
    if (m[2] === "/scans" && method === "GET") return Promise.resolve(json({ runs: [] }));
  }
  return Promise.resolve(json({ error: `unhandled ${method} ${path}` }, 500));
}

function mount(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
  const router = createAppRouter({ queryClient, history: createMemoryHistory({ initialEntries: [path] }) });
  render(
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return router;
}

const mutations = () => fake.calls.filter((c) => c.method !== "GET");

beforeEach(() => {
  fake = { calls: [], projects: [project()], routes: {}, config: {} };
  hard.mockClear();
  window.localStorage.clear();
  useUi.setState({ paletteOpen: false, navOpen: false });
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })),
  );
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

// ---- the /link page ------------------------------------------------------------------

describe("link page", () => {
  const LINK = "/link?platform=https%3A%2F%2Fwhygraph.example.com&org=acme&project=alpha";

  it("link page does not act on load", async () => {
    const user = userEvent.setup();
    mount(LINK);
    // It renders what it was asked to do, in full ...
    expect(await screen.findByTestId("link-platform")).toHaveTextContent("https://whygraph.example.com");
    expect(screen.getByTestId("link-request")).toHaveTextContent("acme");
    await waitFor(() => expect(screen.getByLabelText("Machine name")).toHaveValue("ada-laptop"));
    // ... and has sent nothing but reads: no connect, no callback, no navigation.
    await new Promise((r) => setTimeout(r, 50));
    expect(mutations()).toEqual([]);
    expect(fake.calls.some((c) => c.path.startsWith("/api/platform"))).toBe(false);
    expect(hard).not.toHaveBeenCalled();

    // Only the button talks to the portal, once, and the browser goes where the server said.
    fake.routes["POST /api/platform/connect"] = () =>
      json({ authorize_url: AUTHORIZE, platform_origin: "https://whygraph.example.com", known_platform: false });
    await user.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(AUTHORIZE));
    expect(mutations()).toEqual([
      {
        method: "POST",
        path: "/api/platform/connect",
        body: { platform_url: "https://whygraph.example.com", client_name: "ada-laptop", org: "acme", project: "alpha" },
      },
    ]);
  });

  it("offers no Connect for an address that is not a platform", async () => {
    mount("/link?platform=javascript%3Aalert(1)");
    await screen.findByTestId("link-invalid");
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
    expect(mutations()).toEqual([]);
    expect(parseLinkPlatform("https://x.example/path?q=1")).toBe("https://x.example");
    expect(parseLinkPlatform(undefined)).toBeNull();
  });

  it("shows a refused connect through the shared link messages", async () => {
    const user = userEvent.setup();
    fake.routes["POST /api/platform/connect"] = () =>
      json({ detail: "x", code: "bad_platform_url" }, 422);
    mount(LINK);
    await user.click(await screen.findByRole("button", { name: "Connect" }));
    expect(await screen.findByTestId("connect-error")).toHaveTextContent(
      linkError(new ApiError(422, "x", "bad_platform_url")),
    );
  });
});

// ---- the callback page ---------------------------------------------------------------

describe("connect callback page", () => {
  const CB = "/connect/callback?code=C1&state=S1&iss=https%3A%2F%2Fwhygraph.example.com";

  it("posts the query once and continues the wizard with the link", async () => {
    fake.routes["POST /api/platform/callback"] = () => json({ link_id: "L1" });
    fake.routes["GET /api/platform/pending/L1"] = () => json(PENDING);
    const router = mount(CB);
    await screen.findByTestId("platform-picker");
    expect(mutations()).toEqual([
      { method: "POST", path: "/api/platform/callback", body: { state: "S1", iss: "https://whygraph.example.com", code: "C1" } },
    ]);
    expect(router.state.location.pathname).toBe("/projects/new");
    expect(router.state.location.search).toMatchObject({ source: "platform", link: "L1" });
  });

  it("renders a refusal with the shared message", async () => {
    fake.routes["POST /api/platform/callback"] = () => json({ detail: "x", code: "issuer_mismatch" }, 422);
    mount(CB);
    expect(await screen.findByTestId("callback-error")).toHaveTextContent("came from an unexpected server");
    expect(screen.getByRole("link", { name: "Start again" })).toBeInTheDocument();
  });

  it("forwards a cancelled consent as an error", async () => {
    fake.routes["POST /api/platform/callback"] = () => json({ detail: "x", code: "access_denied" }, 409);
    mount("/connect/callback?error=access_denied&state=S1&iss=https%3A%2F%2Fwhygraph.example.com");
    await screen.findByTestId("callback-error");
    expect(mutations()[0].body).toEqual({ state: "S1", iss: "https://whygraph.example.com", error: "access_denied" });
  });
});

// ---- the platform source -------------------------------------------------------------

describe("platform source", () => {
  it("prefills the machine name and sends the form to Connect", async () => {
    const user = userEvent.setup();
    fake.routes["POST /api/platform/connect"] = () =>
      json({ authorize_url: AUTHORIZE, platform_origin: "https://whygraph.example.com", known_platform: true });
    mount("/projects/new?source=platform");
    const name = await screen.findByLabelText("Machine name");
    await waitFor(() => expect(name).toHaveValue("ada-laptop"));
    // Configure is not a step of a linked project.
    const steps = within(screen.getByRole("list", { name: "Steps" }));
    expect(steps.queryByText("Configure")).toBeNull();
    await user.type(screen.getByLabelText("Platform address"), "https://whygraph.example.com");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(AUTHORIZE));
  });

  it("lists candidates and other repos, links the pick and goes to Initialize", async () => {
    const user = userEvent.setup();
    fake.routes["GET /api/platform/pending/L1"] = () => json(PENDING);
    fake.routes["POST /api/projects"] = () =>
      json(
        {
          project: project({ initialized: false, initialized_at: null, last_scan_at: null }),
          detected: { existing_db: false, managed_hooks: [], detected_agents: [], custom_db_paths: [] },
          import: { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] },
        },
        201,
      );
    const router = mount("/projects/new?source=platform&link=L1");
    const picker = await screen.findByTestId("platform-picker");
    expect(within(picker).getByRole("radiogroup", { name: "Checkout" })).toHaveTextContent("/repos/alpha");
    expect(within(picker).getByRole("radiogroup", { name: "Other repositories" })).toHaveTextContent("/repos/other");
    expect(screen.queryByTestId("no-candidates")).toBeNull();
    const submit = screen.getByRole("button", { name: "Link this checkout" });
    expect(submit).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: /alpha/ }));
    await user.click(submit);
    await waitFor(() => expect(router.state.location.pathname).toBe("/p/alpha/init"));
    expect(router.state.location.search).toMatchObject({ step: "setup" });
    expect(mutations().find((c) => c.path === "/api/projects")?.body).toEqual({
      source: "platform",
      link_id: "L1",
      path: "/repos/alpha",
    });
  });

  it("shows the clone command when nothing matches and a refused link", async () => {
    const user = userEvent.setup();
    fake.routes["GET /api/platform/pending/L1"] = () => json({ ...PENDING, candidates: [] });
    fake.routes["POST /api/projects"] = () => json({ detail: "x", code: "origin_mismatch" }, 422);
    mount("/projects/new?source=platform&link=L1");
    expect(await screen.findByTestId("no-candidates")).toHaveTextContent("git clone https://github.com/acme/alpha.git");
    await user.click(screen.getByRole("radio", { name: /other/ }));
    await user.click(screen.getByRole("button", { name: "Link this checkout" }));
    expect(await screen.findByTestId("link-error")).toHaveTextContent("origin is not the platform project");
  });

  it("an expired link says so and offers to start again", async () => {
    fake.routes["GET /api/platform/pending/L1"] = () => json({ detail: "x", code: "link_expired" }, 410);
    mount("/projects/new?source=platform&link=L1");
    expect(await screen.findByTestId("pending-error")).toHaveTextContent("link request expired");
  });

  it("abandoning revokes the pending link", async () => {
    const user = userEvent.setup();
    fake.routes["GET /api/platform/pending/L1"] = () => json(PENDING);
    fake.routes["DELETE /api/platform/pending/L1"] = () => json({ revoked: true });
    mount("/projects/new?source=platform&link=L1");
    await screen.findByTestId("platform-picker");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(mutations().map((c) => `${c.method} ${c.path}`)).toEqual(["DELETE /api/platform/pending/L1"]));
  });

  it("a slug already on this machine blocks the link", async () => {
    fake.routes["GET /api/platform/pending/L1"] = () => json({ ...PENDING, slug_taken: true });
    mount("/projects/new?source=platform&link=L1");
    expect(await screen.findByTestId("slug-taken")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Link this checkout" })).toBeDisabled();
    expect(screen.queryByTestId("reconnect-notice")).toBeNull();
  });

  // The server filters an already-linked checkout out of both lists and names it
  // under `reconnect` instead, so the picker offers it as its own row.
  const RECONNECT = {
    ...PENDING,
    candidates: [],
    other_repos: [],
    reconnect: { path: "/repos/alpha", slug: "alpha" },
  };

  it("offers the already-linked checkout as a reconnect and posts its path", async () => {
    const user = userEvent.setup();
    fake.routes["GET /api/platform/pending/L1"] = () => json(RECONNECT);
    fake.routes["POST /api/projects"] = () =>
      json(
        {
          project: project(),
          detected: { existing_db: false, managed_hooks: [], detected_agents: [], custom_db_paths: [] },
          import: { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] },
        },
        201,
      );
    mount("/projects/new?source=platform&link=L1");
    const notice = await screen.findByTestId("reconnect-notice");
    expect(notice).toHaveTextContent("already linked");
    expect(notice).toHaveTextContent("new connection token");
    // Non-destructive: it is not the collision dead end, and nothing says Remove first.
    expect(screen.queryByTestId("slug-taken")).toBeNull();
    // The row is there, marked, and - being the one sensible pick - selected.
    const group = screen.getByRole("radiogroup", { name: "Checkout" });
    expect(group).toHaveTextContent("/repos/alpha");
    expect(group).toHaveTextContent("already linked");
    expect(within(group).getByRole("radio")).toBeChecked();
    // No "nothing matches" clone block, although the candidate list is empty.
    expect(screen.queryByTestId("no-candidates")).toBeNull();

    const submit = screen.getByRole("button", { name: "Reconnect this checkout" });
    expect(submit).toBeEnabled();
    await user.click(submit);
    await waitFor(() =>
      expect(mutations().find((c) => c.path === "/api/projects")?.body).toEqual({
        source: "platform",
        link_id: "L1",
        path: "/repos/alpha",
      }),
    );
  });

  it("another path than the reconnect target is still an ordinary link", async () => {
    const user = userEvent.setup();
    fake.routes["GET /api/platform/pending/L1"] = () =>
      json({ ...RECONNECT, other_repos: [{ path: "/repos/other", name: "other" }] });
    mount("/projects/new?source=platform&link=L1");
    await screen.findByTestId("reconnect-notice");
    await user.click(screen.getByRole("radio", { name: /other/ }));
    expect(screen.getByRole("button", { name: "Link this checkout" })).toBeEnabled();
  });
});

// ---- the project card ----------------------------------------------------------------

describe("linked project Set up", () => {
  const initBody = (body: Json | null) => {
    const dry = body?.dry_run === true;
    return json({
      dry_run: dry,
      gitignore_added: dry ? [] : [".whygraph/"],
      hooks: dry ? null : { installed: ["post-commit"], removed: [], actions: {} },
      hooks_error: null,
      agent_files: ((body?.agents as string[]) ?? []).map((a) => ({
        file: a === "claude" ? ".mcp.json" : `.${a}/mcp.json`,
        status: "write",
        agent: a,
        reason: null,
        snippet: null,
        diff: null,
      })),
      asset_files: [],
      configured_agents: dry ? [] : (body?.agents ?? []),
      needs_confirmation: [],
      refused: [],
      marker_written: !dry,
      initialized: !dry,
      custom_db_paths: [],
      ...(dry ? {} : { initial_run_id: 5 }),
    });
  };
  const sse = (frames: string[]) => {
    const enc = new TextEncoder();
    return new Response(
      new ReadableStream({
        start(c) {
          for (const f of frames) c.enqueue(enc.encode(f));
          c.close();
        },
      }),
      { status: 200, headers: { "content-type": "text/event-stream" } },
    );
  };

  beforeEach(() => {
    fake.projects = [project({ initialized: false, initialized_at: null, last_scan_at: null })];
    fake.routes["POST /api/projects/alpha/init"] = initBody;
    fake.routes["GET /api/projects/alpha/scans/5"] = () =>
      json({ id: 5, kind: "scan", trigger: "initial", analyze: false, status: "ok", requested_by: null, started_at: null, finished_at: null, summary: null });
    fake.routes["GET /api/projects/alpha/scans/5/events"] = () =>
      sse([
        'id: 1\ndata: {"type":"start","phase_total":1,"phases":["Code index"]}\n\n',
        'id: 2\ndata: {"type":"phase","phase":1,"title":"CodeGraph"}\n\n',
        'id: 3\nevent: end\ndata: {"type":"end","run_id":5,"status":"ok","summary":null}\n\n',
      ]);
  });

  it("is Source -> Set up; with no agent it warns, and Finish asks once more", async () => {
    const user = userEvent.setup();
    const router = mount("/p/alpha/init");
    await waitFor(() => expect(router.state.location.search).toMatchObject({ step: "setup" }));
    const steps = within(await screen.findByRole("list", { name: "Steps" }));
    expect(steps.getAllByRole("listitem").map((li) => li.textContent).filter(Boolean)).toEqual(["Source", "2Set up"]);
    expect(await screen.findByTestId("no-agent-warning")).toHaveTextContent(
      "Pick at least one agent - without one, nothing on this machine uses the link.",
    );
    await user.click(await screen.findByRole("button", { name: "Finish" }));
    expect(mutations().some((c) => c.path === "/api/projects/alpha/init" && c.body?.dry_run !== true)).toBe(false);
    await user.click(screen.getByRole("button", { name: "Finish without an agent" }));
    expect(await screen.findByTestId("init-done")).toHaveTextContent("Project linked");
  });

  it("preselects the agents the checkout has, then ends on its first scan and Connect your agent", async () => {
    window.sessionStorage.setItem(
      "whygraph:detected:alpha",
      JSON.stringify({
        existing_db: false,
        managed_hooks: [],
        detected_agents: [{ agent: "claude", file: ".mcp.json", key: "mcpServers.whygraph", shape: "http", stale: false, tracked: false }],
        custom_db_paths: [],
      }),
    );
    const user = userEvent.setup();
    const router = mount("/p/alpha/init?step=setup");
    expect(await screen.findByRole("checkbox", { name: /Claude Code/ })).toBeChecked();
    expect(screen.queryByTestId("no-agent-warning")).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Finish" }));
    expect(await screen.findByRole("heading", { name: "First scan complete" })).toBeInTheDocument();
    expect(screen.getByTestId("connect-agent")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open project" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/p/alpha"));
    window.sessionStorage.clear();
  });
});

describe("linked project card", () => {
  const cases: [LinkStatus, string | null, string][] = [
    ["ok", null, "Linked to acme/alpha on whygraph.example.com"],
    ["removed", "project_deleted", "Removed on whygraph.example.com"],
    ["revoked", "admin_revoked", "Access revoked (Revoked by an admin)"],
    ["unreachable", null, "Platform unreachable"],
    ["update_required", null, "Update WhyGraph"],
    ["access_lost", null, "Linked to acme/alpha on whygraph.example.com"],
  ];
  it.each(cases)("says the right thing for %s", async (status, reason, text) => {
    fake.projects = [project({ link: link({ status, status_reason: reason }) })];
    mount("/");
    const notice = await screen.findByTestId("link-notice");
    expect(notice).toHaveAttribute("data-status", status);
    expect(notice).toHaveTextContent(text);
  });

  it("offers reconnect and remove when revoked, remove when removed", async () => {
    fake.projects = [project({ link: link({ status: "revoked", status_reason: "idle" }) })];
    mount("/");
    const notice = await screen.findByTestId("link-notice");
    expect(within(notice).getByRole("link", { name: "Reconnect" }).getAttribute("href")).toContain("/link?");
    expect(within(notice).getByRole("link", { name: "Remove from this machine" })).toBeInTheDocument();
  });

  it("offers only remove when the project access was removed", async () => {
    fake.projects = [project({ link: link({ status: "revoked", status_reason: "project_access_removed" }) })];
    mount("/");
    const notice = await screen.findByTestId("link-notice");
    expect(notice).toHaveTextContent("Access revoked (Your access to the project was removed)");
    expect(notice).toHaveTextContent("Ask a project admin on whygraph.example.com for access");
    expect(within(notice).queryByRole("link", { name: "Reconnect" })).toBeNull();
    expect(within(notice).getByRole("link", { name: "Remove from this machine" })).toBeInTheDocument();
  });

  it("degrades when a platform project has no link", async () => {
    fake.projects = [project({ link: undefined })];
    mount("/");
    expect(await screen.findByTestId("link-notice")).toHaveTextContent("Linked to a platform project");
  });
});

// ---- project home, Explorer, settings ------------------------------------------------

describe("linked project pages", () => {
  it("home points at the platform and has no local Explorer, stats or estimate", async () => {
    mount("/p/alpha");
    const panel = await screen.findByTestId("linked-panel");
    const manage = within(panel).getByRole("link", { name: /Manage on platform/ });
    expect(manage).toHaveAttribute("href", "https://acme.whygraph.example.com/p/alpha/settings");
    expect(within(panel).getByRole("link", { name: /Open Explorer on platform/ })).toHaveAttribute(
      "href",
      "https://acme.whygraph.example.com/p/alpha/explorer",
    );
    expect(within(panel).getByRole("link", { name: /Open Chat on platform/ })).toBeInTheDocument();
    expect(within(panel).getByTestId("linked-role")).toHaveTextContent("Contributor");
    expect(screen.queryByRole("link", { name: "Open Explorer" })).toBeNull();
    expect(screen.queryByTestId("stats")).toBeNull();
    expect(fake.calls.some((c) => c.path.endsWith("/scan-estimate"))).toBe(false);
  });

  it("a revoked link offers Reconnect only: no Rescan, no Manage, no Connect your agent (OVW-3)", async () => {
    fake.projects = [project({ link: link({ status: "revoked", status_reason: "idle" }) })];
    mount("/p/alpha");
    const health = await screen.findByTestId("health-panel");
    expect(health).toHaveAttribute("data-status", "link_revoked");
    expect(screen.getByTestId("health-link")).toHaveTextContent("Access revoked");
    const reconnect = screen.getAllByRole("link", { name: "Reconnect" });
    expect(reconnect[0].getAttribute("href")).toContain("/link?");
    expect(screen.queryByRole("button", { name: "Rescan" })).toBeNull();
    expect(within(screen.getByTestId("linked-panel")).queryByRole("link", { name: /Manage on platform/ })).toBeNull();
    expect(screen.queryByTestId("connect-agent")).toBeNull();
    expect(screen.getByText("Link revoked")).toBeInTheDocument();
  });

  it("a healthy linked project offers a plain Rescan, never a full one", async () => {
    mount("/p/alpha");
    await screen.findByTestId("linked-panel");
    expect(screen.getByRole("button", { name: "Rescan" })).toBeEnabled();
    expect(screen.queryByRole("menuitem", { name: "Full rescan" })).toBeNull();
  });

  it("a viewer on the platform sees the role and no chat link", async () => {
    fake.projects = [project({ link: link({ project_role: "viewer" }) })];
    mount("/p/alpha");
    const panel = await screen.findByTestId("linked-panel");
    expect(within(panel).getByTestId("linked-role")).toHaveTextContent("Viewer");
    expect(within(panel).getByRole("link", { name: /Open Explorer on platform/ })).toBeInTheDocument();
    expect(within(panel).queryByRole("link", { name: /Open Chat on platform/ })).toBeNull();
  });

  it("a role not reported yet shows no role line", async () => {
    fake.projects = [project({ link: link({ project_role: null }) })];
    mount("/p/alpha");
    const panel = await screen.findByTestId("linked-panel");
    expect(within(panel).queryByTestId("linked-role")).toBeNull();
    expect(within(panel).getByRole("link", { name: /Open Chat on platform/ })).toBeInTheDocument();
  });

  it("the local Explorer URL shows the platform notice and reads no data", async () => {
    mount("/p/alpha/explorer");
    await screen.findByTestId("linked-elsewhere");
    expect(fake.calls.filter((c) => /\/(tree|search|graph|chat)/.test(c.path))).toEqual([]);
  });

  it("settings are read-only except hooks, which save [scan].hooks alone", async () => {
    const user = userEvent.setup();
    fake.config = { scan: { forge: "off" } };
    mount("/p/alpha/settings");
    expect(await screen.findByTestId("linked-name")).toHaveTextContent("Alpha");
    expect(screen.queryByRole("button", { name: "Rename" })).toBeNull();
    expect(screen.queryByText("Models and keys")).toBeNull();
    // The platform owns the rest, and says so (SET-4, plan section 0.3 #42).
    expect(screen.getByTestId("settings-managed")).toHaveTextContent("Change its settings there.");
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(nav).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "General",
      "Agents",
      "Git hooks",
      "Danger zone",
    ]);
    expect(screen.queryByTestId("key-anthropic")).toBeNull();
    await user.click(await screen.findByRole("checkbox", { name: "post-commit" }));
    await user.click(within(screen.getByRole("region", { name: "Git hooks" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mutations().some((c) => c.method === "PUT")).toBe(true));
    // Nothing but [scan].hooks goes back: the seeded scan.forge is a key a
    // linked project's PUT allowlist does not hold, and the portal refuses a
    // layer carrying one (422) rather than dropping it, which would fail the
    // whole save.
    expect(mutations().find((c) => c.method === "PUT")?.body).toEqual({
      config: { scan: { hooks: ["post-merge", "post-rewrite", "post-checkout"] } },
    });
  });

  it("removing says when the token could not be revoked and links the account page", async () => {
    const user = userEvent.setup();
    fake.routes["DELETE /api/projects/alpha"] = () =>
      json({
        removed: "alpha",
        hooks: null,
        agent_files: [],
        checkout_deleted: false,
        warnings: [],
        token_revoked: false,
        token_revoke_result: "unreachable",
      });
    mount("/p/alpha/settings");
    await user.click(await screen.findByRole("button", { name: "Remove from this machine" }));
    const dialog = await screen.findByTestId("remove-dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove from this machine" }));
    const failed = await screen.findByTestId("revoke-failed");
    expect(within(failed).getByRole("link")).toHaveAttribute("href", "https://whygraph.example.com/account");
  });

  it("a token the platform had already revoked shows no warning (BUG-7)", async () => {
    const user = userEvent.setup();
    fake.routes["DELETE /api/projects/alpha"] = () =>
      json({
        removed: "alpha",
        hooks: null,
        agent_files: [],
        checkout_deleted: false,
        warnings: [],
        token_revoked: false,
        token_revoke_result: "already_revoked",
      });
    mount("/p/alpha/settings");
    await user.click(await screen.findByRole("button", { name: "Remove from this machine" }));
    const dialog = await screen.findByTestId("remove-dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove from this machine" }));
    await screen.findByTestId("remove-done");
    expect(screen.queryByTestId("revoke-failed")).toBeNull();
  });
});

// ---- pure helpers --------------------------------------------------------------------

describe("platform link helpers", () => {
  it("only plain web addresses are rendered as links", () => {
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref("data:text/html,x")).toBeUndefined();
    expect(safeHref("https://a.example/x")).toBe("https://a.example/x");
    expect(accountUrl("https://a.example/some/path")).toBe("https://a.example/account");
    expect(accountUrl(null)).toBeUndefined();
  });

  it("every status has wording, an unknown link degrades", () => {
    for (const s of ["ok", "access_lost", "removed", "revoked", "unreachable", "update_required"] as LinkStatus[]) {
      expect(linkNotice(link({ status: s })).title).not.toBe("");
    }
    expect(linkNotice(null).title).toBe("Linked to a platform project");
  });
});
