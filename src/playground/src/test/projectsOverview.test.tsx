import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CodegraphOverview, ProjectDetails, ProjectOverview, ProjectSummary } from "../api";
import { PROJECT_ACTIONS, type ProjectAction } from "../lib/permissions";
import { projectHealth, showScannedAgo } from "../lib/projectHealth";
import { scanAvailability } from "../lib/scanAvailability";
import { agentChart, agentKindLabel, coverageChart } from "../lib/overview";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

// M2f-3 S16: the shared status precedence, the scan rules, the Projects list
// (stats, cost, role, sort / search, empty states) and the Overview's cards.

// The charts load lazily; capture what reaches ECharts without a canvas.
const seen = vi.hoisted(() => ({ options: [] as Array<Record<string, unknown>> }));
vi.mock("echarts-for-react/esm/core", () => ({
  default: (props: { option: Record<string, unknown> }) => {
    seen.options.push(props.option);
    return <div data-testid="echart" />;
  },
}));
vi.mock("../components/charts/echarts", () => ({ default: {} }));

type Json = Record<string, unknown>;

const OLD = "2026-01-02T00:00:00+00:00";

function summary(over: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    slug: "alpha",
    name: "Alpha",
    source: "local",
    root: "/repos/alpha",
    remote_url: null,
    initialized: true,
    initialized_at: "2026-01-01T00:00:00+00:00",
    last_scan_at: OLD,
    created_at: "2026-01-01T00:00:00+00:00",
    root_status: "ok",
    running_scan: null,
    restricted: false,
    my_role: "admin",
    permissions: PROJECT_ACTIONS,
    last_scan_status: "ok",
    stale: null,
    source_supported: true,
    access_lost: false,
    access_lost_reason: null,
    github_full_name: null,
    installation_account: null,
    ...over,
  };
}

function details(over: Partial<ProjectDetails> = {}): ProjectDetails {
  return {
    ...summary(over),
    agents: [],
    missing_key: null,
    mcp_url: "http://127.0.0.1:8765/mcp/alpha",
    stats: { commits: 1240, described: 471, described_pct: 38, pull_requests: 0, issues: 0, rationale_cards: 52 },
    ...over,
  } as ProjectDetails;
}

const perms = (...actions: ProjectAction[]) => actions;
const CONTRIBUTOR = perms("project.read", "project.chat", "project.scan");
const VIEWER = perms("project.read");
const MY_MACHINE = { uid: "m1", client_name: "my-laptop", created_at: "2026-10-01T00:00:00Z", last_used_at: "2026-10-09T00:00:00Z" };

function overview(over: Partial<ProjectOverview> = {}): ProjectOverview {
  return {
    coverage: { points: [] },
    events: [],
    last_failure: null,
    usage: null,
    agents: {
      days: [{ day: "2026-10-09", mcp: 0, agent: 0 }],
      total_calls: 0,
      llm_calls: 0,
      llm_cost_usd: null,
      by_kind: [],
      people: null,
      connections: null,
      last_call_day: null,
    },
    ...over,
  };
}

// ---- the precedence ---------------------------------------------------------------

describe("projectHealth precedence (plan section 4.10)", () => {
  const link = {
    platform_origin: "https://whygraph.example.com",
    org: "acme",
    remote_slug: "alpha",
    status_reason: "idle",
    last_platform_head: null,
    project_role: "admin" as const,
    explorer_url: "https://acme.whygraph.example.com/p/alpha/explorer",
    chat_url: "https://acme.whygraph.example.com/p/alpha/chat",
    manage_url: "https://acme.whygraph.example.com/p/alpha/settings",
  };
  const cases: [string, Partial<ProjectDetails>, string, string][] = [
    ["healthy", {}, "ready", "Ready"],
    ["a revoked link over Ready", { source: "platform", link: { ...link, status: "revoked" } }, "link_revoked", "Link revoked"],
    ["a removed link", { source: "platform", link: { ...link, status: "removed" } }, "link_revoked", "Link revoked"],
    [
      "access lost over Scan failed",
      { source: "github", access_lost: true, access_lost_reason: "no_access", last_scan_status: "failed" },
      "no_access",
      "No access",
    ],
    [
      "importing over folder missing",
      { source: "github", importing: true, initialized: false, root_status: "missing", running_scan: { id: 5, status: "running", trigger: "initial" } },
      "importing",
      "Importing",
    ],
    [
      "import failed over folder missing",
      { source: "github", importing: true, initialized: false, root_status: "missing" },
      "import_failed",
      "Import failed",
    ],
    ["folder missing", { root_status: "missing" }, "folder_missing", "Folder missing"],
    ["not set up", { initialized: false, last_scan_at: null }, "needs_setup", "Needs setup"],
    ["scanning over failed", { last_scan_status: "failed", running_scan: { id: 2, status: "running", trigger: "manual" } }, "scanning", "Scanning"],
    ["a project budget stop", { llm_block: "budget_exceeded", llm_block_scope: "project" }, "stopped", "Stopped"],
    ["failed", { last_scan_status: "failed" }, "failed", "Scan failed"],
    ["behind", { stale: { commits_behind: 4 } }, "behind", "Behind"],
    ["unsupported", { source: "github", source_supported: false }, "unsupported", "Not supported"],
  ];
  it.each(cases)("%s", (_, over, key, label) => {
    expect(projectHealth(details(over)).status).toMatchObject({ key, label });
  });

  it("outdated agent files are a low-severity item that leaves the pill alone", () => {
    const h = projectHealth(
      details({ agent_files: { installed_version: "2.0.0", current_version: "2.1.0", outdated: true } }),
    );
    expect(h.status.key).toBe("ready");
    expect(h.items.map((i) => [i.id, i.tone])).toContainEqual(["agent_files", "info"]);
    expect(projectHealth(details({ agent_files: { installed_version: "2.1.0", current_version: "2.1.0", outdated: false } })).items).toEqual([]);
  });

  it("names the budget with a tooltip, and a member's own budget is not the project's state", () => {
    expect(projectHealth(details({ llm_block: "budget_exceeded", llm_block_scope: "org" })).status.tooltip).toBe(
      "Monthly budget reached",
    );
    const member = projectHealth(details({ llm_block: "budget_exceeded", llm_block_scope: "member" }));
    expect(member.status.key).toBe("ready");
    expect(member.items.find((i) => i.id === "budget")?.detail).toMatch(/Your monthly budget/);
    // A project hard stop seen through `usage` alone.
    const usage = { month_spend_usd: 20, budget: { monthly_usd: 20, hard_stop: true }, pct: 100 };
    expect(projectHealth(details({ usage })).status.key).toBe("stopped");
  });

  it("orders the items: setup not finished, missing key, waiting descriptions; Healthy is empty", () => {
    expect(projectHealth(details()).items).toEqual([]);
    const notSetUp = projectHealth(details({ initialized: false, last_scan_at: null }));
    expect(notSetUp.items.map((i) => i.title)).toEqual(["Setup not finished"]);
    const busy = projectHealth(details({ missing_key: "anthropic", last_scan_status: "failed" }), {
      waiting: 12,
      lastFailure: { run_id: 9, at: OLD, message: "git fetch failed" },
    });
    expect(busy.items.map((i) => i.id)).toEqual(["missing_key", "failed", "waiting"]);
    expect(busy.items[0].title).toBe("No Anthropic key");
    // The payload names the provider, not the tasks: no task is named (Chat may run
    // on another provider), and no "a Anthropic".
    expect(busy.items[0].detail).toBe(
      "A model this project uses runs on Anthropic, and no Anthropic API key is set, so the features on that model cannot run.",
    );
    expect(busy.items[0].detail).not.toMatch(/Chat|\ba Anthropic/);
    expect(busy.items[1]).toMatchObject({ detail: "git fetch failed" });
    expect(busy.items[1].actions.map((a) => a.label)).toEqual(["Retry", "Open log"]);
    expect(busy.items[1].actions[1].runId).toBe(9);
    expect(busy.items[2].title).toBe("12 commits have no description yet");
  });

  it("asks a project admin when the caller cannot fix it", () => {
    const h = projectHealth(details({ missing_key: "openai", permissions: CONTRIBUTOR }), { waiting: 1 });
    expect(h.items[0]).toMatchObject({ title: "No OpenAI key", actions: [] });
    expect(h.items[0].detail).toMatch(/Ask a project admin/);
    expect(h.items[1]).toMatchObject({ title: "1 commit has no description yet", actions: [] });
  });

  it("hides 'Scanned' while scanning, importing or without a folder", () => {
    expect(showScannedAgo(summary())).toBe(true);
    expect(showScannedAgo(summary({ root_status: "missing" }))).toBe(false);
    expect(showScannedAgo(summary({ running_scan: { id: 1, status: "running", trigger: "manual" } }))).toBe(false);
    expect(showScannedAgo(summary({ importing: true }))).toBe(false);
  });
});

describe("scanAvailability (SCN-7, CN-6)", () => {
  it("allows both for a healthy project and an admin", () => {
    expect(scanAvailability(summary())).toEqual({ quick: { allowed: true }, full: { allowed: true }, retryImport: false });
  });

  it.each<[string, Partial<ProjectSummary>, string]>([
    ["not set up", { initialized: false }, "Finish setting up this project first"],
    ["folder missing", { root_status: "missing" }, "The project folder is missing"],
    ["access lost", { access_lost: true, access_lost_reason: "no_access" }, "GitHub no longer lets WhyGraph read this repository"],
    ["unsupported", { source_supported: false }, "This source is no longer supported in local mode"],
    ["importing", { importing: true, initialized: false, running_scan: { id: 1, status: "running", trigger: "initial" } }, "The import is still running"],
  ])("refuses both when %s", (_, over, reason) => {
    const a = scanAvailability(summary(over));
    expect(a.quick).toEqual({ allowed: false, reason });
    expect(a.full.allowed).toBe(false);
  });

  it("turns a failed import into Retry import", () => {
    const a = scanAvailability(summary({ importing: true, initialized: false }));
    expect(a).toMatchObject({ retryImport: true, quick: { allowed: true }, full: { allowed: false } });
  });

  it("gives a full-only reason per role, link, budget and key", () => {
    expect(scanAvailability(summary({ permissions: CONTRIBUTOR })).full.reason).toBe("Needs the project Admin role");
    expect(scanAvailability(summary({ permissions: VIEWER })).quick.reason).toBe("Needs the project Contributor role");
    expect(scanAvailability(summary({ source: "platform" })).full.reason).toBe("Descriptions run on the platform");
    expect(scanAvailability(summary({ llm_block: "budget_exceeded" })).full.reason).toBe("Monthly budget reached");
    expect(scanAvailability(summary(), { analyzeMissingKey: "openrouter" }).full.reason).toBe("No OpenRouter key");
    expect(scanAvailability(summary({ llm_block: "budget_exceeded" })).quick.allowed).toBe(true);
  });
});

describe("Overview chart data", () => {
  it("puts an event on its run's point, or the first point after it", () => {
    const chart = coverageChart({
      coverage: {
        points: [
          { run_id: 1, at: "2026-10-01T10:00:00Z", commits: 10, described: 0, described_pct: 0, rationale_cards: 0 },
          { run_id: 3, at: "2026-10-03T10:00:00Z", commits: 10, described: 5, described_pct: 50, rationale_cards: 2 },
        ],
      },
      events: [
        { run_id: 1, at: "2026-10-01T10:00:00Z", kind: "import" },
        { run_id: 2, at: "2026-10-02T10:00:00Z", kind: "failed" },
      ],
    })!;
    expect(chart.kind).toBe("line");
    expect(chart.rows.map((r) => r[1])).toEqual([0, 50]);
    expect(chart.markers?.map((m) => [m.x, m.label, m.tone])).toEqual([
      [chart.rows[0][0], "Import", "info"],
      [chart.rows[1][0], "Failed run", "error"],
    ]);
    expect(coverageChart({ coverage: { points: [] }, events: [] })).toBeNull();
  });

  it("calls a local first scan a first scan, not an import, with the info tone", () => {
    const point = (run_id: number, at: string) => ({ run_id, at, commits: 4, described: 0, described_pct: 0, rationale_cards: 0 });
    const chart = coverageChart({
      coverage: { points: [point(1, "2026-10-01T10:00:00Z"), point(2, "2026-10-02T10:00:00Z")] },
      events: [
        { run_id: 1, at: "2026-10-01T10:00:00Z", kind: "first_scan" },
        { run_id: 2, at: "2026-10-02T10:00:00Z", kind: "import" },
      ],
    })!;
    expect(chart.markers?.map((m) => [m.label, m.tone])).toEqual([
      ["First scan", "info"],
      ["Import", "info"],
    ]);
  });

  it("labels same-minute scans by their seconds, never with a numbered repeat", () => {
    const point = (run_id: number, at: string) => ({ run_id, at, commits: 4, described: 0, described_pct: 0, rationale_cards: 0 });
    const chart = coverageChart({
      coverage: {
        points: [
          point(1, "2026-10-09T09:00:00Z"),
          point(2, "2026-10-10T13:17:05Z"),
          point(3, "2026-10-10T13:17:40Z"),
          point(4, "2026-10-10T13:17:40Z"),
        ],
      },
      events: [],
    })!;
    const labels = chart.rows.map((r) => String(r[0]));
    expect(new Set(labels).size).toBe(4);
    expect(labels.some((l) => /\(\d\)/.test(l))).toBe(false);
    // A different day keeps the bare date; the same minute shows its seconds.
    expect(labels[0]).not.toMatch(/\d:\d\d/);
    expect(labels[1]).toMatch(/:05/);
    expect(labels[2]).toMatch(/:40/);
    // The same second reads the same: only an invisible suffix tells them apart.
    expect(labels[3].replace(/\u200b/g, "")).toBe(labels[2]);
  });

  it("draws one agent series per mode, and labels call kinds", () => {
    const days = [{ day: "2026-10-09", mcp: 4, agent: 7 }];
    expect(agentChart(days, false)).toMatchObject({ title: "MCP calls", columns: ["Day", "MCP calls"] });
    expect(agentChart(days, false).rows[0][1]).toBe(4);
    expect(agentChart(days, true)).toMatchObject({ title: "Calls from connected machines" });
    expect(agentChart(days, true).rows[0][1]).toBe(7);
    expect(agentKindLabel("whygraph_evidence_for")).toBe("Evidence lookups");
    expect(agentKindLabel("v1:pr")).toBe("Pull request lookups");
    expect(agentKindLabel("something_new")).toBe("something_new");
    expect(agentKindLabel("codegraph_explore")).toBe("Code exploration");
    expect(agentKindLabel("codegraph_node")).toBe("Symbol and file reads");
    expect(agentKindLabel("codegraph_search")).toBe("Symbol search");
    expect(agentKindLabel("codegraph_impact")).toBe("Change impact");
  });
});

// ---- the hosted CodeGraph block (plan section 4.2c) ---------------------------------------

function cg(over: Partial<CodegraphOverview> = {}): CodegraphOverview {
  return {
    server: { state: "live", pid: 1234, since: new Date(Date.now() - 12 * 60_000).toISOString(), shared: true, available: true },
    enabled: true,
    last_call_at: null,
    last_sync: { at: new Date(Date.now() - 3000).toISOString(), duration_ms: 612, result: "ok", changed_files: 3 },
    sync_failures_10m: 0,
    sync_paused_until: null,
    guard: { ok: true, paths: [], checked_at: "2026-10-09T10:00:00Z" },
    index: { present: true, files: 1050 },
    withheld_tools: [],
    last_index_failure: null,
    since_start: { calls: 20, waited: 12, still_updating: 0, refresh_failed: 0, busy: 0, refused: 1, timeouts: 2, sum_wait_ms: 0, max_wait_ms: 0 },
    ...over,
  };
}

describe("projectHealth codegraph items", () => {
  const ids = (codegraph: CodegraphOverview | null) =>
    projectHealth(details(), { codegraph }).items.map((i) => [i.id, i.tone]);

  it("a live server and a fresh index add nothing, and no block adds nothing", () => {
    expect(ids(cg())).toEqual([]);
    expect(ids(null)).toEqual([]);
  });

  it.each(["restart_window", "unavailable"] as const)("%s is an error with Restart and Open log", (state) => {
    const h = projectHealth(details(), { codegraph: cg({ server: { ...cg().server, state } }) });
    expect(h.items[0]).toMatchObject({ id: "codegraph", tone: "error", title: "CodeGraph is paused on this portal (every project)" });
    expect(h.items[0].actions.map((a) => a.kind)).toEqual(["codegraph_restart", "codegraph_log"]);
    // No new status pill.
    expect(h.status.key).toBe("ready");
  });

  it("a viewer is offered the log but not the restart", () => {
    const h = projectHealth(details({ permissions: VIEWER }), { codegraph: cg({ server: { ...cg().server, state: "unavailable" } }) });
    expect(h.items[0].actions.map((a) => a.kind)).toEqual(["codegraph_log"]);
  });

  it("lists the symlink paths, a failed refresh, withheld tools, the off switch and a missing binary", () => {
    const h = projectHealth(details(), {
      codegraph: cg({
        guard: { ok: false, paths: ["docs/out"], checked_at: "x" },
        last_sync: { at: "2026-10-09T10:00:00Z", duration_ms: 1, result: "failed" },
        withheld_tools: ["codegraph_files"],
        enabled: false,
        server: { ...cg().server, available: false, state: "off" },
      }),
    });
    expect(h.items.map((i) => i.title)).toEqual([
      "A symbolic link leaves the repository",
      "The code index could not be refreshed",
      "CodeGraph tool(s) withheld: codegraph_files",
      "Hosted CodeGraph tools are off (Settings > Scanning)",
      "CodeGraph binary not found on this portal",
    ]);
    expect(h.items[0].detail).toContain("docs/out");
    expect(h.items[1].actions.map((a) => a.label)).toEqual(["Rescan", "Open CodeGraph log"]);
    expect(h.items.slice(3).map((i) => i.tone)).toEqual(["info", "info"]);
    expect(new Set(h.items.map((i) => i.id)).size).toBe(h.items.length);
  });

  it("sits after a failed scan and before behind", () => {
    const h = projectHealth(details({ last_scan_status: "failed", stale: { commits_behind: 2 } }), {
      codegraph: cg({ server: { ...cg().server, state: "unavailable" } }),
    });
    expect(h.items.map((i) => i.id)).toEqual(["failed", "codegraph", "behind"]);
  });
});

// ---- pages against a fake portal ------------------------------------------------------

interface Fake {
  state: Json;
  projects: ProjectSummary[];
  details: Record<string, ProjectDetails>;
  overview: ProjectOverview | { status: number; body: Json };
  estimate: Json | { status: number; body: Json };
  /** `GET /api/projects/{slug}/connections/mine`. */
  mine: Json[];
  log: { method: string; path: string; body: Json | null }[];
}
let fake: Fake;

const LOCAL_STATE: Json = {
  mode: "local",
  setup_complete: true,
  user: { uid: "u1", display_name: "Ada", role: "owner" },
  port: 8765,
  shared_folders: ["/repos"],
  port_change: null,
};

function orgState(role: string): Json {
  return {
    mode: "production",
    host_kind: "org",
    base_url: "http://whygraph.localhost:8765",
    setup_complete: true,
    bootstrap_required: false,
    user: { uid: "u1", display_name: "Ada", email: null, role: null, is_instance_admin: role === "reader", github_login: "ada" },
    org: { slug: "acme", name: "Acme", role },
  };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function reply(out: unknown): Response {
  if (out && typeof out === "object" && "status" in out && "body" in out && typeof (out as { status: unknown }).status === "number") {
    const r = out as { status: number; body: unknown };
    return json(r.body, r.status);
  }
  return json(out);
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const path = url.pathname;
  const method = init?.method ?? "GET";
  const body = init?.body ? (JSON.parse(String(init.body)) as Json) : null;
  fake.log.push({ method, path: path + url.search, body });
  if (path === "/api/portal/state") return Promise.resolve(json(fake.state));
  if (path === "/api/projects" && method === "GET") return Promise.resolve(json({ projects: fake.projects }));
  const m = /^\/api\/projects\/([^/]+)(\/.*)?$/.exec(path);
  if (m) {
    const [, slug, rest] = m;
    const d = fake.details[slug] ?? (fake.projects.find((p) => p.slug === slug) as ProjectDetails | undefined);
    if (!d) return Promise.resolve(json({ error: "not found", code: "not_found" }, 404));
    if (!rest) return Promise.resolve(json(d));
    if (rest === "/scans" && method === "GET") return Promise.resolve(json({ runs: [] }));
    if (rest === "/scans" && method === "POST") return Promise.resolve(json({ run_id: 7 }, 202));
    if (rest === "/codegraph/log") return Promise.resolve(json({ text: "server started\n", size: 15, truncated: false }));
    if (rest === "/codegraph/restart" && method === "POST") return Promise.resolve(json({ was: "unavailable", shared: true }));
    if (rest === "/overview") return Promise.resolve(reply(fake.overview));
    if (rest === "/scan-estimate") return Promise.resolve(reply(fake.estimate));
    if (rest === "/connections/mine") return Promise.resolve(json(fake.mine));
    if (rest.endsWith("/events")) return Promise.resolve(new Response("", { status: 200, headers: { "content-type": "text/event-stream" } }));
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
const here = (r: ReturnType<typeof mount>) => r.state.location.pathname + r.state.location.searchStr;

beforeEach(() => {
  seen.options.length = 0;
  fake = {
    state: LOCAL_STATE,
    projects: [],
    details: {},
    overview: overview(),
    estimate: { status: 404, body: { error: "no estimate" } },
    mine: [],
    log: [],
  };
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

describe("Projects list", () => {
  it("shows stats, cost with a budget bar, the role badge, and the source badge only when sources mix", async () => {
    fake.projects = [
      summary({
        last_scan_stats: { commits: 1240, described_pct: 38.2, rationale_cards: 52, as_of: OLD },
        usage: { month_spend_usd: 12.4, budget: { monthly_usd: 50, hard_stop: false }, pct: 24.8 },
      }),
      summary({ slug: "beta", name: "Beta", root: "/repos/team/beta", my_role: "contributor", permissions: CONTRIBUTOR }),
    ];
    mount("/");
    const alpha = await screen.findByTestId("project-alpha");
    expect(within(alpha).getByTestId("card-stats")).toHaveTextContent("1,240 commits · 38% described · 52 symbols explained");
    expect(within(alpha).getByTestId("card-cost")).toHaveTextContent("$12 this month of $50");
    expect(within(alpha).getByRole("progressbar", { name: "Monthly budget used" })).toHaveAttribute("aria-valuenow", "25");
    // A repo directly in the shared folder would only repeat its name: no subtitle;
    // a nested one shows the path relative to the shared folder.
    expect(within(alpha).queryByTestId("card-subtitle")).toBeNull();
    expect(within(screen.getByTestId("project-beta")).getByTestId("card-subtitle")).toHaveTextContent("team/beta");
    expect(within(alpha).queryByTestId("role-badge")).toBeNull();
    expect(within(screen.getByTestId("project-beta")).getByTestId("role-badge")).toHaveTextContent("Contributor");
    expect(screen.queryByTestId("source-badge")).toBeNull();
    expect(screen.getByTestId("projects-count")).toHaveTextContent("2 projects");
    // New project stays in the header.
    expect(screen.getByRole("link", { name: "New project" })).toHaveAttribute("href", "/projects/new");
  });

  it("shows no cost on a card with nothing spent and no budget (a project that needs setup)", async () => {
    fake.projects = [
      summary({ initialized: false, initialized_at: null, last_scan_at: null, usage: { month_spend_usd: 0, budget: null, pct: null } }),
      summary({ slug: "beta", name: "Beta", usage: { month_spend_usd: 0, budget: { monthly_usd: 20, hard_stop: false }, pct: 0 } }),
      summary({ slug: "gamma", name: "Gamma", usage: { month_spend_usd: 1.25, budget: null, pct: null } }),
    ];
    mount("/");
    const alpha = await screen.findByTestId("project-alpha");
    expect(within(alpha).queryByTestId("card-cost")).toBeNull();
    expect(alpha).not.toHaveTextContent("this month");
    expect(within(screen.getByTestId("project-beta")).getByTestId("card-cost")).toHaveTextContent("$0.00 this month of $20");
    expect(within(screen.getByTestId("project-gamma")).getByTestId("card-cost")).toHaveTextContent("$1.25 this month");
  });

  it("hides Scanned on a scanning card and shows the pill instead", async () => {
    fake.projects = [summary({ running_scan: { id: 3, status: "running", trigger: "manual" } })];
    mount("/");
    const card = await screen.findByTestId("project-alpha");
    expect(card).toHaveTextContent("Scanning");
    expect(card).not.toHaveTextContent("Scanned");
  });

  it("holds the count line with a placeholder while the list loads (ER-5)", async () => {
    mount("/");
    expect(await screen.findByTestId("projects-count-loading")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("projects-count")).toHaveTextContent(/\d+ projects?/));
    expect(screen.queryByTestId("projects-count-loading")).toBeNull();
  });

  it("searches and sorts through the URL", async () => {
    fake.projects = [
      summary({ slug: "zeta", name: "Zeta", last_scan_at: "2026-10-05T00:00:00Z" }),
      summary({ slug: "alpha", name: "Alpha", last_scan_at: OLD, last_scan_status: "failed" }),
      summary({ slug: "mid", name: "Mid", last_scan_at: "2026-10-01T00:00:00Z" }),
    ];
    const user = userEvent.setup();
    const router = mount("/?sort=scanned");
    await screen.findByTestId("project-zeta");
    const order = () => screen.getAllByTestId(/^project-(zeta|alpha|mid)$/).map((el) => el.dataset.testid);
    expect(order()).toEqual(["project-zeta", "project-mid", "project-alpha"]);

    await user.selectOptions(screen.getByRole("combobox", { name: "Sort projects" }), "status");
    await waitFor(() => expect(here(router)).toBe("/?sort=status"));
    expect(order()[0]).toBe("project-alpha");
    // No Cost option without any usage on the cards.
    expect(screen.queryByRole("option", { name: "Cost" })).toBeNull();

    await user.type(screen.getByRole("searchbox", { name: "Search projects" }), "mi");
    await waitFor(() => expect(here(router)).toBe("/?q=mi&sort=status"));
    expect(order()).toEqual(["project-mid"]);
  });

  it("renders 60 cards, then Show more", async () => {
    fake.projects = Array.from({ length: 65 }, (_, i) =>
      summary({ slug: `p${String(i).padStart(2, "0")}`, name: `P${String(i).padStart(2, "0")}` }),
    );
    const user = userEvent.setup();
    mount("/");
    await screen.findByTestId("project-p00");
    expect(screen.getAllByTestId(/^project-p\d\d$/)).toHaveLength(60);
    await user.click(screen.getByRole("button", { name: /Show more/ }));
    expect(screen.getAllByTestId(/^project-p\d\d$/)).toHaveLength(65);
  });

  it("offers the rescan items with descriptions and reasons in the card menu", async () => {
    fake.projects = [summary({ llm_block: "budget_exceeded", llm_block_scope: "org" })];
    const user = userEvent.setup();
    mount("/");
    await user.click(await screen.findByRole("button", { name: "Actions for Alpha" }));
    const quick = await screen.findByRole("menuitem", { name: "Quick rescan" });
    expect(quick).toHaveAccessibleDescription("Git history and the code index, no LLM cost");
    const full = screen.getByRole("menuitem", { name: "Full rescan" });
    expect(full).toHaveAttribute("aria-disabled", "true");
    expect(full).toHaveAccessibleDescription("Monthly budget reached");
    expect(screen.getByRole("menuitem", { name: "Settings" })).toBeInTheDocument();
    await user.click(quick);
    await waitFor(() =>
      expect(fake.log.find((c) => c.method === "POST")?.body).toEqual({ trigger: "manual", analyze: false }),
    );
  });

  it("the empty state for the local user keeps 'No projects yet' and asks for the onboarding items", async () => {
    mount("/");
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    expect(screen.getByTestId("first-run-slot")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add project" })).toHaveAttribute("href", "/projects/new");
    expect(screen.getByRole("link", { name: "New project" })).toBeInTheDocument();
    await waitFor(() => expect(fake.log.some((c) => c.path.startsWith("/api/onboarding"))).toBe(true));
  });

  it("a production password owner sees no Import button, and the explainer once the checklist is not showing", async () => {
    fake.state = {
      ...orgState("owner"),
      user: { uid: "u1", display_name: "Ada", email: "ada@example.com", role: null, is_instance_admin: false, github_login: null, has_password: true },
    };
    mount("/");
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    const box = await screen.findByTestId("github-required");
    expect(within(screen.getByTestId("first-run-slot")).getByTestId("github-required")).toBe(box);
    expect(screen.queryByRole("link", { name: /^Import/ })).toBeNull();
    expect(screen.queryByRole("link", { name: "New project" })).toBeNull();
  });

  it.each([
    ["member", "No projects shared with you yet", "An owner or admin can give you access."],
    ["reader", "No projects", "This organization has no projects."],
  ])("the empty state for a %s", async (role, title, text) => {
    fake.state = orgState(role);
    mount("/");
    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Import/ })).toBeNull();
    expect(fake.log.some((c) => c.path.startsWith("/api/onboarding"))).toBe(false);
  });

  it("names a production project by its GitHub repository, never a server path", async () => {
    fake.state = orgState("owner");
    fake.projects = [summary({ source: "github", root: null, github_full_name: "acme/alpha", remote_url: "https://github.com/acme/alpha" })];
    mount("/");
    const card = await screen.findByTestId("project-alpha");
    expect(within(card).getByTestId("card-subtitle")).toHaveTextContent("acme/alpha");
    expect(card).not.toHaveTextContent("/data/");
    expect(screen.getByRole("link", { name: "Import" })).toHaveAttribute("href", "/projects/new");
  });
});

describe("Overview", () => {
  it("shows the role, the health line, five tiles with definitions and why a zero is zero", async () => {
    fake.details.alpha = details({
      my_role: "contributor",
      permissions: CONTRIBUTOR,
      remote_url: "https://github.com/acme/alpha",
    });
    mount("/p/alpha");
    expect(await screen.findByTestId("my-role")).toHaveTextContent("Your role: Contributor");
    expect(await screen.findByTestId("health-ok")).toHaveTextContent("Healthy");
    const stats = screen.getByTestId("stats");
    expect(within(stats).getByTestId("stat-described")).toHaveTextContent("38%");
    expect(within(stats).getByTestId("stat-described")).toHaveTextContent("471 of 1,240");
    expect(within(stats).getByTestId("stat-pull-requests")).toHaveTextContent("No GitHub data: add a GitHub token");
    expect(within(stats).getByTestId("stat-explained")).toHaveTextContent("52");
    // The fifth tile spans both phone columns.
    expect(within(stats).getByTestId("stat-explained")).toHaveClass("col-span-2");
    // A repo directly in the shared folder: the subtitle would only repeat the name.
    expect(screen.queryByTestId("overview-subtitle")).toBeNull();
    expect(screen.getByRole("button", { name: "About Symbols explained" })).toBeInTheDocument();
    // A contributor gets a plain Rescan; no Full rescan to offer.
    expect(screen.getByRole("button", { name: "Rescan" })).toBeEnabled();
  });

  it("shows the CodeGraph server line, the tallies, the log and a restart that names the shared server", async () => {
    const user = userEvent.setup();
    fake.details.alpha = details();
    fake.overview = overview({
      codegraph: cg({ server: { ...cg().server, state: "unavailable" } }),
      agents: {
        ...overview().agents,
        total_calls: 3,
        by_kind: [{ kind: "codegraph_explore", calls: 3 }],
        last_call_day: "2026-10-09",
      },
    });
    mount("/p/alpha");
    const server = await screen.findByTestId("codegraph-server");
    expect(server).toHaveTextContent("CodeGraph server: unavailable · pid 1234");
    expect(server).toHaveTextContent("index refreshed just now (0.6 s)");
    expect(screen.getByTestId("codegraph-tallies")).toHaveTextContent(
      "12 calls waited for the index, 2 timed out, 1 refused (since the portal started).",
    );
    expect(screen.getByTestId("agent-kinds")).toHaveTextContent("Code exploration");

    await user.click(screen.getByRole("button", { name: "Open CodeGraph log" }));
    expect(await screen.findByTestId("codegraph-log-dialog")).toHaveTextContent("server started");
    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("button", { name: "Restart CodeGraph" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("restarts the CodeGraph server for every project on this portal");
    await user.click(within(dialog).getByRole("button", { name: "Restart CodeGraph" }));
    await waitFor(() =>
      expect(fake.log.some((c) => c.method === "POST" && c.path === "/api/projects/alpha/codegraph/restart")).toBe(true),
    );
  });

  it("says history starts with the next scan, and shows no usage card without usage", async () => {
    fake.details.alpha = details();
    mount("/p/alpha");
    expect(await screen.findByTestId("coverage-empty")).toHaveTextContent("History starts with the next scan");
    expect(screen.queryByTestId("usage-card")).toBeNull();
    // No agent call in 30 days: the setup card is the agent section.
    expect(screen.getByTestId("connect-agent")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-activity")).toBeNull();
  });

  it("draws coverage with markers, usage by task and agent activity with people and connections", async () => {
    fake.state = orgState("owner");
    fake.mine = [MY_MACHINE];
    fake.details.alpha = details({ source: "github", root: null, github_full_name: "acme/alpha", mcp_url: null });
    fake.overview = overview({
      coverage: {
        points: [
          { run_id: 1, at: "2026-10-01T10:00:00Z", commits: 10, described: 0, described_pct: 0, rationale_cards: 0 },
          { run_id: 2, at: "2026-10-03T10:00:00Z", commits: 10, described: 5, described_pct: 50, rationale_cards: 2 },
        ],
      },
      events: [{ run_id: 2, at: "2026-10-03T10:00:00Z", kind: "full_scan" }],
      usage: { month: "2026-10", spent_usd: 3.5, budget_usd: 10, pct: 35, by_task: [{ task: "analyze", calls: 40, cost_usd: 3.5 }] },
      agents: {
        days: [
          { day: "2026-10-08", mcp: 0, agent: 2 },
          { day: "2026-10-09", mcp: 0, agent: 3 },
        ],
        total_calls: 5,
        llm_calls: 2,
        llm_cost_usd: 0.12,
        by_kind: [{ kind: "v1:evidence", calls: 4 }],
        people: [{ label: "Ada (@ada)", calls: 5, last_day: "2026-10-09" }],
        connections: [{ client_name: "ada-laptop", user_label: "Ada", last_used_at: "2026-10-09T10:00:00Z" }],
        last_call_day: "2026-10-09",
      },
    });
    mount("/p/alpha");
    expect(await screen.findByTestId("github-link")).toHaveAttribute("href", "https://github.com/acme/alpha");
    await waitFor(() => expect(screen.getAllByTestId("echart")).toHaveLength(2));
    const coverage = seen.options.find((o) => (o.series as Array<{ type: string }>)[0].type === "line")!;
    const series = coverage.series as Array<{ name?: string; data: unknown[] }>;
    expect(series).toHaveLength(2);
    expect(series[1].data[0]).toBeNull();
    expect(series[1].data[1]).toMatchObject({ value: 50 });
    expect(screen.getByTestId("usage-card")).toHaveTextContent("$3.50");
    expect(screen.getByTestId("usage-card")).toHaveTextContent("Commit descriptions · 40 calls");
    const agents = screen.getByTestId("agent-activity");
    expect(within(agents).getByTestId("agent-summary")).toHaveTextContent("5 agent calls in 30 days, 2 used the LLM (~$0.12)");
    expect(within(agents).getByTestId("agent-kinds")).toHaveTextContent("Evidence lookups");
    expect(within(agents).getByTestId("agent-people")).toHaveTextContent("Ada (@ada)");
    expect(within(agents).getByTestId("agent-connections")).toHaveTextContent("ada-laptop");
    // An admin gets one machine list (every member's, their own included), never a second one.
    expect(within(agents).queryByTestId("my-machines")).toBeNull();
    // Production's series is the calls from connected machines.
    const bars = seen.options.find((o) => (o.series as Array<{ type: string }>)[0].type === "bar")!;
    expect((bars.series as Array<{ data: unknown[] }>)[0].data).toEqual([2, { value: 3, label: expect.anything() }]);
    // The server path is never printed in production.
    expect(screen.getByTestId("overview-subtitle")).toHaveTextContent("acme/alpha");
    expect(screen.getByTestId("overview-subtitle")).not.toHaveTextContent("/data/");
  });

  describe("the project's agent section in production, with calls", () => {
    const called = () =>
      overview({
        agents: {
          days: [{ day: "2026-10-09", mcp: 0, agent: 2 }],
          total_calls: 2,
          llm_calls: 0,
          llm_cost_usd: null,
          by_kind: [],
          people: null,
          connections: null,
          last_call_day: "2026-10-09",
        },
      });
    const open = async (label: string) => {
      const agents = await screen.findByTestId("agent-activity");
      const summary = within(agents).getByText(label);
      expect(summary.closest("details")).not.toHaveAttribute("open");
      return { agents, summary };
    };

    it("keeps the machines visible and the closed disclosure mounts nothing until opened", async () => {
      const user = userEvent.setup();
      fake.state = orgState("member");
      fake.mine = [MY_MACHINE];
      fake.details.alpha = details({ source: "github", root: null, github_full_name: "acme/alpha", mcp_url: null });
      fake.overview = called();
      mount("/p/alpha");
      const { agents, summary } = await open("Connect another machine");
      expect(within(agents).getByTestId("my-machines")).toHaveTextContent("Connected on my-laptop");
      expect(within(agents).queryByRole("link", { name: "Open in my local WhyGraph" })).toBeNull();
      expect(within(agents).queryByTestId("local-port-summary")).toBeNull();
      // Closed, it mints no one-time intent either.
      const minted = () => fake.log.some((c) => c.method === "POST" && c.path === "/api/projects/alpha/connect-intent");
      expect(minted()).toBe(false);
      await user.click(summary);
      expect(await within(agents).findByRole("link", { name: "Open in my local WhyGraph" })).toBeInTheDocument();
      await waitFor(() => expect(minted()).toBe(true));
      // Only the steps: no nested card, no second machine list or disclosure.
      expect(within(agents).queryByTestId("use-with-agent")).toBeNull();
      expect(within(agents).queryByTestId("connect-another")).toBeNull();
      expect(within(agents).getAllByTestId("my-machines")).toHaveLength(1);
    });

    it("labels it Connect local WhyGraph when no machine is linked", async () => {
      const user = userEvent.setup();
      fake.state = orgState("member");
      fake.details.alpha = details({ source: "github", root: null, github_full_name: "acme/alpha", mcp_url: null });
      fake.overview = called();
      mount("/p/alpha");
      const { agents, summary } = await open("Connect local WhyGraph");
      expect(within(agents).queryByTestId("my-machines")).toBeNull();
      expect(within(agents).queryByRole("link", { name: "Open in my local WhyGraph" })).toBeNull();
      await user.click(summary);
      expect(await within(agents).findByRole("link", { name: "Open in my local WhyGraph" })).toBeInTheDocument();
    });

    it("a reader's Overview asks for no machine list and shows no setup", async () => {
      fake.state = orgState("reader");
      fake.details.alpha = details({ source: "github", root: null, github_full_name: "acme/alpha", mcp_url: null });
      fake.overview = called();
      mount("/p/alpha");
      await screen.findByTestId("agent-activity");
      expect(fake.log.some((c) => c.path.endsWith("/connections/mine"))).toBe(false);
      expect(fake.log.some((c) => c.path.endsWith("/connect-intent"))).toBe(false);
      expect(screen.queryByText(/Connect (another machine|local WhyGraph)/)).toBeNull();
    });

    it("keeps local mode's Connect another agent, with the same deferral", async () => {
      const user = userEvent.setup();
      fake.details.alpha = details();
      fake.overview = called();
      mount("/p/alpha");
      const { agents, summary } = await open("Connect another agent");
      expect(within(agents).queryByTestId("connect-agent")).toBeNull();
      await user.click(summary);
      expect(await within(agents).findByTestId("connect-agent")).toBeInTheDocument();
      expect(fake.log.some((c) => c.path.endsWith("/connections/mine"))).toBe(false);
    });
  });

  it("a failed last scan offers Retry and Open log from the Overview route's last_failure", async () => {
    fake.details.alpha = details({ last_scan_status: "failed" });
    fake.overview = overview({ last_failure: { run_id: 9, at: OLD, message: "simulated crawler error" } });
    const user = userEvent.setup();
    const router = mount("/p/alpha");
    const failed = await screen.findByTestId("health-failed");
    await waitFor(() => expect(failed).toHaveTextContent("simulated crawler error"));
    expect(within(failed).getByRole("link", { name: "Open log" })).toHaveAttribute("href", "/p/alpha/scans/9");
    await user.click(within(failed).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/scans/7"));
  });

  it("an access-lost project offers only its fix: no Retry, Rescan or Describe anywhere (OVW-3)", async () => {
    fake.details.alpha = details({
      source: "github",
      access_lost: true,
      access_lost_reason: "no_access",
      last_scan_status: "failed",
      stale: { commits_behind: 2 },
    });
    fake.overview = overview({ last_failure: { run_id: 9, at: OLD, message: "simulated crawler error" } });
    fake.estimate = {
      commits: 30,
      upper_bound: true,
      large_commits: 0,
      model: { provider: "anthropic", model: "claude-haiku-4-5" },
      tokens: null,
      cost: null,
      cost_hidden: true,
      missing_key: null,
    };
    mount("/p/alpha");
    expect(await screen.findByTestId("access-lost")).toBeInTheDocument();
    expect(screen.queryByTestId("health-failed")).toBeNull();
    expect(screen.queryByTestId("stale-banner")).toBeNull();
    expect(screen.queryByTestId("health-waiting")).toBeNull();
    expect(screen.queryByTestId("estimate-card")).toBeNull();
    expect(within(screen.getByTestId("health-panel")).queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Describe" })).toBeNull();
    for (const b of screen.queryAllByRole("button", { name: "Rescan" })) expect(b).toBeDisabled();
    const health = projectHealth(details({ access_lost: true, access_lost_reason: "no_access", last_scan_status: "failed" }));
    expect(health.items.flatMap((i) => i.actions.map((a) => a.kind))).not.toContain("retry_scan");
  });

  it("shows waiting commits without a price to a caller without project.usage (R6)", async () => {
    fake.details.alpha = details({ my_role: "contributor", permissions: CONTRIBUTOR });
    fake.estimate = {
      commits: 30,
      upper_bound: true,
      large_commits: 0,
      model: { provider: "anthropic", model: "claude-haiku-4-5" },
      tokens: null,
      cost: null,
      cost_hidden: true,
      missing_key: null,
    };
    mount("/p/alpha");
    const card = await screen.findByTestId("estimate-card");
    expect(card).toHaveTextContent("30 commits are waiting for descriptions. Ask a project admin to describe them.");
    expect(card).not.toHaveTextContent("$");
    expect(screen.getByTestId("health-waiting")).toHaveTextContent("30 commits have no description yet");
  });

  it("a folder-missing project hides the agent snippet and says why Rescan is disabled", async () => {
    fake.details.alpha = details({ root_status: "missing", stats: null });
    mount("/p/alpha");
    expect(await screen.findByTestId("project-unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rescan" })).toBeDisabled();
    expect(screen.getAllByText("The project folder is missing").length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.queryByTestId("agent-activity")).toBeNull());
    expect(screen.queryByTestId("connect-agent")).toBeNull();
    expect(screen.queryByText(/scanned .* ago/)).toBeNull();
  });
});
