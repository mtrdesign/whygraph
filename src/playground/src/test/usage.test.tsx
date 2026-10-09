import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { formatPct, formatTokens, formatUsd } from "../lib/format";
import { addDays, formatResetsAt, matchPreset, monthRange, presetRange } from "../lib/usageRange";
import { AMOUNT_HINT, parseAmount } from "../components/usage/BudgetsTab";
import { parseRates } from "../components/usage/PricesTab";
import { validateUsageSearch } from "../pages/UsagePage";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

// The chart card draws to a canvas; these tests cover the page, not ECharts.
vi.mock("echarts-for-react/esm/core", () => ({ default: () => <div data-testid="echart" /> }));
vi.mock("../components/charts/echarts", () => ({ default: {} }));

// ---- pure helpers ------------------------------------------------------------------

describe("lib/format", () => {
  it("formats money: cents under $10, whole dollars above, <$0.01 for dust", () => {
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(0.004)).toBe("<$0.01");
    expect(formatUsd(0.01)).toBe("$0.01");
    expect(formatUsd(3.267)).toBe("$3.27");
    expect(formatUsd(12.5)).toBe("$13");
    expect(formatUsd(1234.4)).toBe("$1,234");
    expect(formatUsd(null)).toBe("-");
    expect(formatUsd(undefined)).toBe("-");
  });

  it("compacts token counts and passes null through", () => {
    expect(formatTokens(999)).toBe("999");
    expect(formatTokens(12_345)).toBe("12.3k");
    expect(formatTokens(2_000)).toBe("2k");
    expect(formatTokens(1_234_567)).toBe("1.2M");
    expect(formatTokens(null)).toBe("-");
    expect(formatPct(80)).toBe("80%");
    expect(formatPct(12.5)).toBe("12.5%");
    expect(formatPct(null)).toBe("-");
  });
});

describe("lib/usageRange", () => {
  const now = new Date("2026-10-07T12:00:00Z");

  it("builds UTC months with an exclusive end", () => {
    expect(monthRange(0, now)).toEqual({ from: "2026-10-01", to: "2026-11-01" });
    expect(monthRange(-1, now)).toEqual({ from: "2026-09-01", to: "2026-10-01" });
    expect(monthRange(-10, now)).toEqual({ from: "2025-12-01", to: "2026-01-01" });
  });

  it("includes today in the last-N-days presets and recognises them", () => {
    expect(presetRange("7d", now)).toEqual({ from: "2026-10-01", to: "2026-10-08" });
    expect(matchPreset({}, now)).toBe("this_month");
    expect(matchPreset(presetRange("30d", now), now)).toBe("30d");
    expect(matchPreset({ from: "2026-01-01", to: "2026-02-15" }, now)).toBe("custom");
    expect(addDays("2026-02-28", 1)).toBe("2026-03-01");
  });

  it("says when a month resets", () => {
    expect(formatResetsAt("2026-11-01T00:00:00+00:00")).toBe("1 Nov, 00:00 UTC");
  });

  it("validates the /usage address", () => {
    expect(
      validateUsageSearch({ tab: "calls", scan_run: "7", chat_session: "x", sort: "cost", from: "2026-1-1", junk: "1" }),
    ).toEqual({
      tab: "calls",
      from: undefined,
      to: undefined,
      project: undefined,
      member: undefined,
      task: undefined,
      source: undefined,
      model: undefined,
      scan_run: "7",
      chat_session: undefined,
      sort: "cost",
    });
    expect(validateUsageSearch({ tab: "nope" }).tab).toBeUndefined();
  });

  it("validates budget amounts and price rates before sending", () => {
    expect(parseAmount("abc")).toEqual({ error: AMOUNT_HINT });
    expect(parseAmount("0")).toEqual({ error: AMOUNT_HINT });
    expect(parseAmount("2000000")).toEqual({ error: AMOUNT_HINT });
    expect(parseAmount("$1,250.505")).toEqual({ amount: 1250.51 });
    const draft = { input_per_mtok: "3", output_per_mtok: "", cache_read_per_mtok: "", cache_write_per_mtok: "" };
    expect(parseRates(draft)).toMatchObject({ field: "output_per_mtok" });
    expect(parseRates({ ...draft, output_per_mtok: "15", cache_read_per_mtok: "20000" })).toMatchObject({
      field: "cache_read_per_mtok",
    });
    expect(parseRates({ ...draft, output_per_mtok: "15" })).toEqual({
      rates: { input_per_mtok: 3, output_per_mtok: 15, cache_read_per_mtok: null, cache_write_per_mtok: null },
    });
  });
});

// ---- a fake portal -------------------------------------------------------------------

const BASE = "http://whygraph.localhost:8765";

type Handler = (method: string, body: unknown, search: URLSearchParams) => Response;

interface Fake {
  state: Record<string, unknown>;
  calls: { path: string; method: string; body: unknown; search: string }[];
  routes: Record<string, Handler>;
}
let fake: Fake;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const gauge = (over: Record<string, unknown> = {}) => ({
  spent_usd: 12.5,
  budget_usd: 100,
  pct: 12.5,
  hard_stop: false,
  blocked: false,
  ...over,
});
const usageBlock = (me: unknown, org: unknown) => ({
  month: "2026-10",
  resets_at: "2026-11-01T00:00:00+00:00",
  me,
  org,
  projects_over: org ? [] : null,
});

const ada = { uid: "u1", display_name: "Ada", email: null, github_login: "ada", role: null, is_instance_admin: false };

function orgState(role: string, usage: unknown) {
  return {
    mode: "production",
    host_kind: "org",
    base_url: BASE,
    setup_complete: true,
    bootstrap_required: false,
    user: ada,
    org: { slug: "acme", name: "Acme", role },
    usage,
  };
}
const ownerState = () => orgState("owner", usageBlock(gauge(), gauge()));
const adminState = () => orgState("admin", usageBlock(gauge(), gauge()));
const memberState = () => orgState("member", usageBlock(gauge({ budget_usd: 50, pct: 25 }), null));
const readerState = () => orgState("reader", usageBlock(null, gauge()));
const localState = () => ({
  mode: "local",
  setup_complete: true,
  user: { uid: "u1", display_name: "Local", role: "owner" },
  port: 8765,
  shared_folders: [],
  usage: usageBlock(null, gauge()),
});

function project(slug: string) {
  return {
    slug,
    name: `Project ${slug}`,
    source: "local",
    root: `/r/${slug}`,
    remote_url: null,
    initialized: true,
    initialized_at: "2026-01-01T00:00:00Z",
    last_scan_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    root_status: "ok",
    running_scan: null,
    restricted: false,
    my_role: "admin",
    permissions: PROJECT_ACTIONS,
    stale: null,
    agents: [],
    missing_key: null,
    mcp_url: null,
    stats: null,
  };
}

const split = { interactive: { calls: 6, cost_usd: 4.5 }, scans: { calls: 4, cost_usd: 8 } };
const totals = (cost: number, calls = 10) => ({
  calls,
  input_tokens: 1000 * calls,
  output_tokens: 100 * calls,
  cache_read_tokens: null,
  cache_write_tokens: null,
  reasoning_tokens: null,
  cost_usd: cost,
  unpriced_calls: 0,
});
const row = (key: string | number | null, label: string, cost: number, extra: Record<string, unknown> = {}) => ({
  key,
  label,
  ...totals(cost),
  ...split,
  ...extra,
});
const GROUPS: Record<string, unknown[]> = {
  project: [row("alpha", "Project alpha", 10), row("gone", "gone", 2.5)],
  member: [
    row("u2", "Ben (@ben)", 9, { top_project: { slug: "alpha", name: "Project alpha", cost_usd: 9 } }),
    row(null, "System", 3, { top_project: null }),
    row(null, "Former Member", 0.5, { top_project: null }),
  ],
  model: [row("claude-sonnet-4-5", "claude-sonnet-4-5", 12.5)],
  machine: [row(5, "ben-laptop", 2), row(null, "Portal", 10.5)],
  task: [row("chat", "chat", 4), row("analyze", "analyze", 8.5)],
};
function report(search: URLSearchParams) {
  const group = search.get("group");
  return {
    range: { from: "2026-10-01", to: "2026-11-01" },
    totals: { ...totals(12.5), unpriced_calls: 2 },
    split,
    series: [
      { day: "2026-10-01", calls: 3, cost_usd: 1.2, input_tokens: 10, output_tokens: 5 },
      { day: "2026-10-02", calls: 7, cost_usd: 11.3, input_tokens: 20, output_tokens: 9 },
    ],
    groups: group ? (GROUPS[group] ?? []) : [],
  };
}
const call = (id: number, over: Record<string, unknown> = {}) => ({
  id,
  created_at: "2026-10-02T10:00:00+00:00",
  project_slug: "alpha",
  project_name: "Project alpha",
  actor_label: "Ada (@ada)",
  user_uid: "u1",
  source: "chat",
  task: "chat",
  provider: "anthropic",
  model_requested: "claude-sonnet-4-5",
  model_served: "claude-sonnet-4-5",
  key_scope: "org",
  input_tokens: 1200,
  output_tokens: 300,
  cache_read_tokens: null,
  cache_write_tokens: null,
  reasoning_tokens: null,
  cost_usd: 0.0123,
  cost_source: "estimated",
  price_version: "bundled:2026-09-25",
  scan_run_id: null,
  chat_session_id: null,
  client_name: null,
  subject: null,
  ...over,
});

const MEMBERS = [
  { uid: "u1", display_name: "Ada", github_login: "ada", avatar_url: null, role: "admin", joined_at: "2026-01-01", disabled: false },
  { uid: "u2", display_name: "Ben", github_login: "ben", avatar_url: null, role: "member", joined_at: "2026-01-01", disabled: false },
  { uid: "u3", display_name: "Olga", github_login: "olga", avatar_url: null, role: "owner", joined_at: "2026-01-01", disabled: false },
  { uid: "u4", display_name: "Cleo", github_login: "cleo", avatar_url: null, role: "member", joined_at: "2026-01-01", disabled: false },
  { uid: "u5", display_name: "Zed", github_login: "zed", avatar_url: null, role: "owner", joined_at: "2026-01-01", disabled: false },
];
const budget = (monthly: number, spent: number | null, hard = false) => ({
  monthly_usd: monthly,
  hard_stop: hard,
  spent_usd: spent,
  pct: spent === null ? null : Math.round((spent * 1000) / monthly) / 10,
});
const BUDGETS = {
  month: "2026-10",
  resets_at: "2026-11-01T00:00:00+00:00",
  org: budget(100, 80, true),
  member_default: budget(30, null),
  members: [
    { ...budget(20, 5), uid: "u1", label: "Ada (@ada)" },
    { ...budget(25, 9), uid: "u2", label: "Ben (@ben)" },
    { ...budget(40, 1), uid: "u3", label: "Olga (@olga)" },
  ],
  projects: [{ ...budget(40, 10), slug: "alpha", name: "Project alpha" }],
  alerts: [{ scope: "org", label: "Acme", threshold: 75, crossed_at: "2026-10-05T10:00:00+00:00", spent_usd: 76 }],
  unpriced_calls: 3,
};
const PRICES = {
  as_of: "2026-09-25",
  rows: [
    {
      provider: "anthropic",
      model: "claude-sonnet-4-5",
      input_per_mtok: 3,
      output_per_mtok: 15,
      cache_read_per_mtok: 0.3,
      cache_write_per_mtok: 3.75,
      origin: "bundled",
      updated_at: null,
    },
    {
      provider: "openrouter",
      model: "deepseek/deepseek-chat",
      input_per_mtok: 0.5,
      output_per_mtok: 1,
      cache_read_per_mtok: null,
      cache_write_per_mtok: null,
      origin: "override",
      updated_at: "2026-10-01T09:00:00+00:00",
    },
  ],
};

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const path = url.pathname;
  const method = init?.method ?? "GET";
  let body: unknown = undefined;
  try {
    body = init?.body ? JSON.parse(String(init.body)) : undefined;
  } catch {
    body = init?.body;
  }
  fake.calls.push({ path, method, body, search: url.search });
  const route = fake.routes[`${method} ${path}`] ?? fake.routes[path];
  if (route) return Promise.resolve(route(method, body, url.searchParams));
  if (path === "/api/portal/state") return Promise.resolve(json(fake.state));
  if (path === "/api/projects") return Promise.resolve(json({ projects: [project("alpha")] }));
  if (path === "/api/org/members") return Promise.resolve(json(MEMBERS));
  if (path === "/api/usage" || path === "/api/usage/me") return Promise.resolve(json(report(url.searchParams)));
  if (path === "/api/usage/calls" || path === "/api/usage/me/calls") {
    return Promise.resolve(json({ items: [call(1)], next: null }));
  }
  if (path === "/api/budgets") return Promise.resolve(json(BUDGETS));
  if (path === "/api/prices" && method === "GET") return Promise.resolve(json(PRICES));
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
  return { router };
}
const here = (router: ReturnType<typeof mount>["router"]) =>
  router.state.location.pathname + router.state.location.searchStr;
const tabNames = async () => (await screen.findAllByRole("tab")).map((t) => t.textContent);

beforeEach(() => {
  fake = { state: ownerState(), calls: [], routes: {} };
  useUi.setState({ paletteOpen: false, navOpen: false });
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })),
  );
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  // jsdom has no object URLs; the CSV download needs them.
  URL.createObjectURL = vi.fn(() => "blob:x");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---- the page by role and mode -------------------------------------------------------

describe("Usage page tabs by role and mode", () => {
  it("local mode: no Members or Machines tab, and a sidebar item", async () => {
    fake.state = localState();
    mount("/usage");
    expect(await tabNames()).toEqual(["Overview", "Projects", "Models", "Calls", "Budgets", "Prices"]);
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Usage & cost" })).toHaveAttribute("href", "/usage");
    expect(await screen.findByTestId("tile-this-month")).toHaveTextContent("$13");
    expect(screen.getByTestId("estimated-note")).toBeInTheDocument();
    expect(screen.getByTestId("unpriced-note")).toHaveTextContent("2 calls had no price");
    expect(screen.getByTestId("usage-split")).toHaveTextContent("Interactive $4.50");
    // The chart card is lazy; it still arrives.
    expect(await screen.findByTestId("echart")).toBeInTheDocument();
  });

  it("production owners and admins get every tab", async () => {
    fake.state = adminState();
    mount("/usage");
    expect(await tabNames()).toEqual([
      "Overview",
      "Projects",
      "Members",
      "Models",
      "Machines",
      "Calls",
      "Budgets",
      "Prices",
    ]);
    expect(screen.getByRole("link", { name: "My usage" })).toHaveAttribute("href", "/usage/me");
  });

  it("switches tabs through the address", async () => {
    const { router } = mount("/usage");
    await userEvent.click(await screen.findByRole("tab", { name: "Models" }));
    await waitFor(() => expect(here(router)).toBe("/usage?tab=models"));
    expect(await screen.findByTestId("breakdown-model")).toHaveTextContent("claude-sonnet-4-5");
    expect(fake.calls.some((c) => c.path === "/api/usage" && c.search.includes("group=model"))).toBe(true);
  });

  it("sends a production member to their own usage", async () => {
    fake.state = memberState();
    const { router } = mount("/usage");
    expect(await screen.findByTestId("member-usage-page")).toBeInTheDocument();
    expect(here(router)).toBe("/usage/me");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("My usage");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Usage & cost" })).toHaveAttribute("href", "/usage/me");
    expect(await screen.findByTestId("my-budget")).toHaveTextContent("$13 of $50 (25%)");
    await waitFor(() => expect(fake.calls.some((c) => c.path === "/api/usage/me")).toBe(true));
    expect(fake.calls.some((c) => c.path === "/api/usage")).toBe(false);
    expect(fake.calls.some((c) => c.path === "/api/usage/me/calls" && c.search.includes("sort=cost"))).toBe(true);
  });

  it("refuses a member the drill-downs of others", async () => {
    fake.state = memberState();
    mount("/usage/members/u2");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });

  it("is not found without usage, and /usage/me is production only", async () => {
    fake.state = { ...localState(), usage: null };
    mount("/usage");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).queryByRole("link", { name: "Usage & cost" })).toBeNull();
  });

  it("local mode has no My usage page", async () => {
    fake.state = localState();
    mount("/usage/me");
    expect(await screen.findByText("Only on a team portal")).toBeInTheDocument();
  });
});

describe("breakdowns, drill-down and calls", () => {
  it("links a member to the drill-down, System to its calls, and leaves a deleted member as text", async () => {
    mount("/usage?tab=members");
    const table = await screen.findByTestId("breakdown-member");
    expect(within(table).getByRole("link", { name: "Ben (@ben)" })).toHaveAttribute("href", "/usage/members/u2");
    expect(within(table).getByRole("link", { name: "System" })).toHaveAttribute(
      "href",
      "/usage?tab=calls&member=system",
    );
    expect(within(table).queryByRole("link", { name: "Former Member" })).toBeNull();
    expect(table).toHaveTextContent("Project alpha ($9.00)");
  });

  it("links a project row to a filtered Calls view and exports the group as CSV", async () => {
    fake.routes["/api/usage.csv"] = () =>
      new Response("key,label\n", {
        headers: {
          "content-type": "text/csv",
          "content-disposition": 'attachment; filename="whygraph-usage-acme-project.csv"',
          "x-whygraph-truncated": "1",
        },
      });
    mount("/usage?tab=projects&from=2026-09-01&to=2026-10-01");
    const table = await screen.findByTestId("breakdown-project");
    expect(within(table).getByRole("link", { name: "Project alpha" })).toHaveAttribute(
      "href",
      "/usage?tab=calls&from=2026-09-01&to=2026-10-01&project=alpha",
    );
    await userEvent.click(screen.getByTestId("csv-project"));
    expect(await screen.findByTestId("csv-project-truncated")).toHaveTextContent("50,000 rows");
    const csv = fake.calls.find((c) => c.path === "/api/usage.csv");
    expect(csv?.search).toContain("group=project");
    expect(csv?.search).toContain("from=2026-09-01");
  });

  it("machines have no Calls link (no machine filter)", async () => {
    mount("/usage?tab=machines");
    const table = await screen.findByTestId("breakdown-machine");
    expect(table).toHaveTextContent("ben-laptop");
    expect(within(table).queryAllByRole("link")).toHaveLength(0);
  });

  it("filters calls, links a scan run, and links a chat session only for the viewer's own row", async () => {
    fake.routes["/api/usage/calls"] = () =>
      json({
        items: [
          call(1, { chat_session_id: 4 }),
          call(2, { user_uid: "u2", actor_label: "Ben (@ben)", chat_session_id: 9 }),
          call(3, { source: "scan", task: "analyze", scan_run_id: 12, subject: "abc123", cost_source: "unpriced", cost_usd: null }),
        ],
        next: "cursor-2",
      });
    const { router } = mount("/usage?tab=calls&project=alpha");
    expect(await screen.findByTestId("call-chat-1")).toHaveAttribute("href", "/p/alpha/chat/4");
    expect(screen.getByTestId("call-chat-2").tagName).toBe("SPAN");
    expect(screen.getByTestId("call-chat-2")).toHaveTextContent("#9");
    const scanRow = screen.getByTestId("call-3");
    expect(within(scanRow).getByRole("link", { name: "Run #12" })).toHaveAttribute("href", "/p/alpha/scans/12");
    expect(scanRow).toHaveTextContent("unpriced");
    expect(fake.calls.some((c) => c.path === "/api/usage/calls" && c.search.includes("project=alpha"))).toBe(true);

    const filters = screen.getByTestId("calls-filters");
    await userEvent.selectOptions(within(filters).getByLabelText("Sort"), "cost");
    await userEvent.selectOptions(within(filters).getByLabelText("Task"), "analyze");
    await userEvent.click(within(filters).getByRole("button", { name: "Filter" }));
    await waitFor(() => expect(here(router)).toBe("/usage?tab=calls&project=alpha&task=analyze&sort=cost"));
    await waitFor(() =>
      expect(
        fake.calls.some((c) => c.path === "/api/usage/calls" && c.search.includes("sort=cost") && c.search.includes("task=analyze")),
      ).toBe(true),
    );

    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await waitFor(() => expect(fake.calls.some((c) => c.search.includes("before=cursor-2"))).toBe(true));
  });

  it("shows the provider-cost footnote only when a row is starred (BUG-24)", async () => {
    fake.routes["/api/usage/calls"] = () => json({ items: [call(1)], next: null });
    mount("/usage?tab=calls");
    await screen.findByTestId("call-1");
    expect(screen.queryByText("* cost reported by the provider.")).toBeNull();
    cleanup();
    fake.routes["/api/usage/calls"] = () => json({ items: [call(1), call(2, { cost_source: "provider" })], next: null });
    mount("/usage?tab=calls");
    expect(await screen.findByTestId("call-2")).toHaveTextContent("*");
    expect(screen.getByText("* cost reported by the provider.")).toBeInTheDocument();
  });

  it("shows one member's drill-down to an org admin", async () => {
    fake.state = adminState();
    mount("/usage/members/u2");
    const heading = await screen.findByRole("heading", { level: 1 });
    await waitFor(() => expect(heading).toHaveTextContent("Ben"));
    expect(screen.getByRole("link", { name: "All their calls" })).toHaveAttribute(
      "href",
      "/usage?tab=calls&member=u2",
    );
    // The header crumb names the member.
    const header = screen.getByRole("banner");
    expect(await within(header).findByText("Ben")).toBeInTheDocument();
    expect(within(header).getByRole("link", { name: "Usage & cost" })).toHaveAttribute("href", "/usage");
    await waitFor(() =>
      expect(fake.calls.some((c) => c.path === "/api/usage" && c.search.includes("member=u2") && c.search.includes("group=task"))).toBe(true),
    );
    expect(fake.calls.some((c) => c.path === "/api/usage/calls" && c.search.includes("member=u2") && c.search.includes("sort=cost"))).toBe(true);
    expect(await screen.findByTestId("breakdown-task")).toHaveTextContent("Chat");
  });
});

// ---- budgets -------------------------------------------------------------------------

describe("budget editor", () => {
  it("makes an admin's own row and owners' rows read-only, and keeps them out of the picker", async () => {
    fake.state = adminState();
    mount("/usage?tab=budgets");
    const own = await screen.findByTestId("budget-member-u1");
    expect(within(own).queryByRole("textbox")).toBeNull();
    expect(own).toHaveTextContent("Only an owner can change your own budget.");
    const owner = screen.getByTestId("budget-member-u3");
    expect(within(owner).queryByRole("textbox")).toBeNull();
    expect(owner).toHaveTextContent("Only an owner can change an owner's budget.");
    expect(within(screen.getByTestId("budget-member-u2")).getByRole("textbox")).toHaveValue("25");
    const picker = within(screen.getByTestId("add-member-budget")).getByLabelText("Member");
    const options = within(picker).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Choose…", "Cleo (@cleo)"]);
    expect(screen.getByTestId("unpriced-note")).toHaveTextContent("3 calls had no price and are not counted toward budgets");
    expect(screen.getByTestId("budget-alerts")).toHaveTextContent("75%");
    expect(screen.getByTestId("budget-org-spent")).toHaveTextContent("$80 of $100 (80%)");
  });

  it("lets an owner edit every member row", async () => {
    mount("/usage?tab=budgets");
    expect(within(await screen.findByTestId("budget-member-u3")).getByRole("textbox")).toBeInTheDocument();
    const picker = within(screen.getByTestId("add-member-budget")).getByLabelText("Member");
    expect(within(picker).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Choose…",
      "Cleo (@cleo)",
      "Zed (@zed)",
    ]);
  });

  it("validates the amount before sending", async () => {
    mount("/usage?tab=budgets");
    const org = await screen.findByTestId("budget-org");
    const input = within(org).getByRole("textbox");
    await userEvent.clear(input);
    await userEvent.type(input, "abc");
    await userEvent.click(within(org).getByRole("button", { name: "Save" }));
    expect(await within(org).findByRole("alert")).toHaveTextContent(AMOUNT_HINT);
    expect(fake.calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("shows budget_above_org and budget_below_children inline", async () => {
    fake.routes["PUT /api/projects/alpha/budget"] = () =>
      json({ error: "too high", code: "budget_above_org", org_monthly_usd: 100 }, 422);
    fake.routes["PUT /api/budgets/org"] = () =>
      json(
        {
          error: "too low",
          code: "budget_below_children",
          children: [{ scope: "project", monthly_usd: 40, slug: "alpha", name: "Project alpha" }],
        },
        409,
      );
    mount("/usage?tab=budgets");
    const projectRow = await screen.findByTestId("budget-project-alpha");
    const amount = within(projectRow).getByRole("textbox");
    await userEvent.clear(amount);
    await userEvent.type(amount, "150");
    await userEvent.click(within(projectRow).getByRole("button", { name: "Save" }));
    expect(await within(projectRow).findByRole("alert")).toHaveTextContent(
      "at or below the organization's budget ($100)",
    );
    expect(fake.calls.find((c) => c.method === "PUT")?.body).toEqual({ monthly_usd: 150, hard_stop: false });

    const org = screen.getByTestId("budget-org");
    const orgAmount = within(org).getByRole("textbox");
    await userEvent.clear(orgAmount);
    await userEvent.type(orgAmount, "10");
    await userEvent.click(within(org).getByRole("button", { name: "Save" }));
    expect(await within(org).findByRole("alert")).toHaveTextContent("Project alpha ($40)");
  });

  it("saves a hard stop and refreshes the banners' state", async () => {
    fake.routes["PUT /api/budgets/members/u2"] = (_m, body) => json({ ...(body as object), spent_usd: 9, pct: 36 });
    mount("/usage?tab=budgets");
    const ben = await screen.findByTestId("budget-member-u2");
    await userEvent.click(within(ben).getByRole("switch"));
    await userEvent.click(within(ben).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(fake.calls.find((c) => c.method === "PUT" && c.path === "/api/budgets/members/u2")?.body).toEqual({
        monthly_usd: 25,
        hard_stop: true,
      }),
    );
    // The state (banners) and the budgets are fetched again.
    await waitFor(() => expect(fake.calls.filter((c) => c.path === "/api/budgets").length).toBeGreaterThan(1));
    expect(fake.calls.filter((c) => c.path === "/api/portal/state").length).toBeGreaterThan(1);
  });

  it("is read-only for a reader", async () => {
    fake.state = readerState();
    mount("/usage?tab=budgets");
    const tab = await screen.findByTestId("budgets-tab");
    await within(tab).findByTestId("budget-org");
    expect(within(tab).queryAllByRole("textbox")).toHaveLength(0);
    expect(within(tab).queryByRole("button", { name: /Save|Remove|Add budget/ })).toBeNull();
    expect(tab).toHaveTextContent("Owners and org admins change budgets.");
  });

  it("has no member budgets in local mode", async () => {
    fake.state = localState();
    mount("/usage?tab=budgets");
    expect(await screen.findByTestId("budget-projects")).toBeInTheDocument();
    expect(screen.queryByTestId("budget-members")).toBeNull();
    expect(within(screen.getByTestId("budget-org")).getByRole("textbox")).toBeInTheDocument();
  });
});

// ---- prices --------------------------------------------------------------------------

describe("prices", () => {
  it("overrides a bundled row and reverts an override", async () => {
    fake.routes["PUT /api/prices"] = (_m, body) => json({ ...(body as object), origin: "override", updated_at: "now" });
    fake.routes["DELETE /api/prices"] = () => new Response(null, { status: 204 });
    mount("/usage?tab=prices");
    expect(await screen.findByTestId("prices-as-of")).toHaveTextContent("2026-09-25");
    const bundled = screen.getByTestId("price-anthropic-claude-sonnet-4-5");
    expect(bundled).toHaveAttribute("data-origin", "bundled");
    expect(within(bundled).queryByRole("button", { name: "Revert" })).toBeNull();
    await userEvent.click(within(bundled).getByRole("button", { name: "Override" }));
    const form = await screen.findByTestId("price-form-anthropic-claude-sonnet-4-5");
    const input = within(form).getByLabelText("Input");
    expect(input).toHaveValue("3");
    await userEvent.clear(input);
    await userEvent.type(input, "2.5");
    await userEvent.click(within(form).getByRole("button", { name: "Save override" }));
    await waitFor(() =>
      expect(fake.calls.find((c) => c.method === "PUT" && c.path === "/api/prices")?.body).toEqual({
        provider: "anthropic",
        model: "claude-sonnet-4-5",
        input_per_mtok: 2.5,
        output_per_mtok: 15,
        cache_read_per_mtok: 0.3,
        cache_write_per_mtok: 3.75,
      }),
    );

    const override = screen.getByTestId("price-openrouter-deepseek/deepseek-chat");
    await userEvent.click(within(override).getByRole("button", { name: "Revert" }));
    await waitFor(() => {
      const del = fake.calls.find((c) => c.method === "DELETE" && c.path === "/api/prices");
      expect(new URLSearchParams(del?.search).get("model")).toBe("deepseek/deepseek-chat");
      expect(new URLSearchParams(del?.search).get("provider")).toBe("openrouter");
    });
  });

  it("shows invalid_price inline", async () => {
    fake.routes["PUT /api/prices"] = () =>
      json({ error: "bad", code: "invalid_price", field: "output_per_mtok" }, 422);
    mount("/usage?tab=prices");
    const add = await screen.findByTestId("add-price");
    await userEvent.type(within(add).getByLabelText("Model"), "deepseek-chat");
    await userEvent.type(within(add).getByLabelText("Input"), "1");
    await userEvent.type(within(add).getByLabelText("Output"), "2");
    await userEvent.click(within(add).getByRole("button", { name: "Add price" }));
    expect(await within(add).findByRole("alert")).toHaveTextContent("The output price must be a price per million tokens");
  });

  it("is read-only for a reader", async () => {
    fake.state = readerState();
    mount("/usage?tab=prices");
    expect(await screen.findByTestId("price-table")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Override" })).toBeNull();
    expect(screen.queryByTestId("add-price")).toBeNull();
  });
});
