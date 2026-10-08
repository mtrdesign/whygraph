import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { RationaleTab } from "../components/RationaleTab";
import { MessageThread } from "../components/chat/MessageThread";
import { turnsFromMessages } from "../components/chat/turns";
import { ProjectAccess } from "../components/portal/ProjectAccess";
import { ProjectUsageSection } from "../components/usage/ProjectUsageSection";
import { MEMBER_STOPPED_LINE, budgetNoticeText, thresholdOf } from "../lib/budgetBanner";
import { authMessage } from "../lib/authErrors";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { ProjectProvider } from "../lib/project";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

// The chart card draws to a canvas; these tests cover the surfaces, not ECharts.
vi.mock("echarts-for-react/esm/core", () => ({ default: () => <div data-testid="echart" /> }));
vi.mock("../components/charts/echarts", () => ({ default: {} }));

const BASE = "http://whygraph.localhost:8765";

type Handler = (method: string, body: unknown, search: URLSearchParams) => Response;
interface Fake {
  state: Record<string, unknown>;
  routes: Record<string, Handler>;
  calls: { path: string; method: string; body: unknown }[];
}
let fake: Fake;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const sse = (frames: unknown[]) => {
  const enc = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(c) {
        for (const f of frames) c.enqueue(enc.encode(`data: ${JSON.stringify(f)}\n\n`));
        c.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
};

const gauge = (over: Record<string, unknown> = {}) => ({
  spent_usd: 12.5,
  budget_usd: 100,
  pct: 12.5,
  hard_stop: false,
  blocked: false,
  ...over,
});
const usageBlock = (me: unknown, org: unknown, projects_over: unknown = org ? [] : null) => ({
  month: "2026-10",
  resets_at: "2026-11-01T00:00:00+00:00",
  me,
  org,
  projects_over,
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

function project(slug: string, over: Record<string, unknown> = {}) {
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
    ...over,
  };
}

const MEMBERS = [
  { uid: "u1", display_name: "Ada", github_login: "ada", avatar_url: null, role: "admin", joined_at: "2026-01-01", disabled: false },
  { uid: "u2", display_name: "Ben", github_login: "ben", avatar_url: null, role: "member", joined_at: "2026-01-01", disabled: false },
];

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
  fake.calls.push({ path, method, body });
  const route = fake.routes[`${method} ${path}`] ?? fake.routes[path];
  if (route) return Promise.resolve(route(method, body, url.searchParams));
  if (path === "/api/portal/state") return Promise.resolve(json(fake.state));
  if (path === "/api/projects") return Promise.resolve(json({ projects: [project("alpha")] }));
  if (path === "/api/projects/alpha") return Promise.resolve(json(project("alpha")));
  if (path === "/api/org/members") return Promise.resolve(json(MEMBERS));
  if (path === "/api/org/invitations") return Promise.resolve(json([]));
  return Promise.resolve(json({ error: `unhandled ${method} ${path}` }, 404));
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
}
function mount(path: string) {
  const queryClient = client();
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
function inProject(node: React.ReactNode) {
  render(
    <ThemeProvider>
      <QueryClientProvider client={client()}>
        <ProjectProvider slug="alpha">{node}</ProjectProvider>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

beforeEach(() => {
  fake = { state: orgState("member", usageBlock(null, null)), routes: {}, calls: [] };
  useUi.setState({ paletteOpen: false, navOpen: false });
  try {
    window.localStorage.clear();
  } catch {
    // jsdom always has it
  }
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })),
  );
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

// ---- banners ------------------------------------------------------------------------

describe("thresholdOf", () => {
  it("is the highest reached threshold", () => {
    expect(thresholdOf(null)).toBeNull();
    expect(thresholdOf(49.9)).toBeNull();
    expect(thresholdOf(50)).toBe(50);
    expect(thresholdOf(80)).toBe(75);
    expect(thresholdOf(250)).toBe(100);
  });
});

describe("BudgetBanner", () => {
  const memberAt = (pct: number, over: Record<string, unknown> = {}) =>
    orgState("member", usageBlock(gauge({ budget_usd: 50, spent_usd: (pct / 100) * 50, pct, ...over }), null));

  it("shows nothing for a member under 50% or without a budget", async () => {
    fake.state = memberAt(49);
    mount("/");
    await screen.findByRole("heading", { name: "Projects" });
    expect(screen.queryByTestId("budget-banner-me")).toBeNull();
  });

  it("a member at 50% sees it on any org page and can dismiss it for the month", async () => {
    fake.state = memberAt(50);
    const user = userEvent.setup();
    mount("/members");
    const banner = await screen.findByTestId("budget-banner-me");
    expect(banner).toHaveTextContent("50% of your monthly budget");
    expect(banner).toHaveAttribute("data-threshold", "50");
    await user.click(within(banner).getByRole("button", { name: "Dismiss for this month" }));
    expect(screen.queryByTestId("budget-banner-me")).toBeNull();
    expect(window.localStorage.getItem("whygraph:budget-banner:me:2026-10:50")).toBe("1");
  });

  it("stays dismissed on a reload within the month, but 75% is a new notice", async () => {
    window.localStorage.setItem("whygraph:budget-banner:me:2026-10:50", "1");
    fake.state = memberAt(55);
    mount("/members");
    await screen.findByRole("heading", { name: "Members" });
    expect(screen.queryByTestId("budget-banner-me")).toBeNull();
    cleanup();
    fake.state = memberAt(80);
    mount("/members");
    await waitFor(() => expect(screen.getAllByTestId("budget-banner-me").length).toBeGreaterThan(0));
    expect(screen.getAllByTestId("budget-banner-me")[0]).toHaveAttribute("data-threshold", "75");
  });

  it("a member at 100% with a hard stop gets the roadmap line and no dismiss", async () => {
    fake.state = memberAt(100, { hard_stop: true, blocked: true });
    mount("/members");
    const banner = await screen.findByTestId("budget-banner-me");
    expect(banner).toHaveTextContent(MEMBER_STOPPED_LINE);
    expect(banner).toHaveAttribute("data-threshold", "100");
    expect(within(banner).queryByRole("button", { name: "Dismiss for this month" })).toBeNull();
  });

  it("owners and admins get one org banner on Projects and Usage, naming projects at or over 50%", async () => {
    fake.state = orgState(
      "owner",
      usageBlock(null, gauge({ budget_usd: 100, spent_usd: 80, pct: 80 }), [
        { slug: "alpha", name: "Project alpha", pct: 100 },
        { slug: "beta", name: "Project beta", pct: 55 },
      ]),
    );
    mount("/");
    const banner = await screen.findByTestId("budget-banner-org");
    expect(banner).toHaveTextContent("Acme is at 75% of its monthly budget");
    expect(banner).toHaveTextContent("Project alpha (100%), Project beta (50%)");
    expect(banner).toHaveAttribute("data-threshold", "100");
    expect(within(banner).queryByRole("button")).toBeNull();
  });

  it("the org banner is not on other pages, and a member never sees it", async () => {
    fake.state = orgState("admin", usageBlock(null, gauge({ pct: 90, spent_usd: 90 })));
    mount("/members");
    await screen.findByRole("heading", { name: "Members" });
    expect(screen.queryByTestId("budget-banner-org")).toBeNull();
  });

  it("local mode: the org banner shows on Projects", async () => {
    fake.state = {
      mode: "local",
      setup_complete: true,
      user: { uid: "u1", display_name: "Local", role: "owner" },
      port: 8765,
      shared_folders: [],
      usage: usageBlock(null, gauge({ pct: 76, spent_usd: 76 })),
    };
    mount("/");
    expect(await screen.findByTestId("budget-banner-org")).toHaveTextContent("75%");
  });
});

// ---- the project pages and Chat -----------------------------------------------------

describe("budget_exceeded on a project", () => {
  const blocked = (scope = "project") =>
    project("alpha", { llm_block: "budget_exceeded", llm_block_scope: scope });

  it("shows a notice on the project pages, not just for Chat", async () => {
    fake.routes["/api/projects/alpha"] = () => json(blocked("org"));
    mount("/p/alpha");
    const notice = await screen.findByTestId("budget-notice");
    expect(notice).toHaveTextContent("This organization's monthly LLM budget is reached");
    expect(notice).toHaveTextContent("You can still read everything that's already generated.");
  });

  it("a normal project has no notice", async () => {
    mount("/p/alpha");
    await screen.findByRole("heading", { name: "Project alpha" });
    expect(screen.queryByTestId("budget-notice")).toBeNull();
  });

  it("the Generate button is disabled with the reason", async () => {
    fake.routes["/api/projects/alpha"] = () => json(blocked("member"));
    fake.routes["/api/projects/alpha/node/rationale"] = () => json({ status: "missing" });
    inProject(<RationaleTab qualifiedName="a.b" />);
    const button = await screen.findByRole("button", { name: "Generate rationale" });
    expect(button).toBeDisabled();
    expect(await screen.findByTestId("generate-blocked")).toHaveTextContent(MEMBER_STOPPED_LINE);
  });

  it("Generate stays enabled without a block", async () => {
    fake.routes["/api/projects/alpha/node/rationale"] = () => json({ status: "missing" });
    inProject(<RationaleTab qualifiedName="a.b" />);
    expect(await screen.findByRole("button", { name: "Generate rationale" })).toBeEnabled();
    expect(screen.queryByTestId("generate-blocked")).toBeNull();
  });
});

describe("Chat under a hard stop", () => {
  const msg = (id: number, role: string, content: string, over: Record<string, unknown> = {}) => ({
    id,
    role,
    content,
    tool_calls: [],
    tool_call_id: null,
    input_tokens: null,
    output_tokens: null,
    provider: "openai",
    model: "gpt",
    error: null,
    created_at: "2026-10-01T00:00:00Z",
    ...over,
  });
  const transcript = (messages: unknown[]) => ({
    id: 1,
    title: "t",
    provider: "openai",
    model: "gpt",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    messages,
  });

  it("replaces the composer with the notice, keeping the history readable", async () => {
    fake.routes["/api/projects/alpha"] = () => json(project("alpha", { llm_block: "budget_exceeded", llm_block_scope: "project" }));
    fake.routes["/api/projects/alpha/chat/sessions/1"] = () =>
      json(transcript([msg(1, "user", "why?"), msg(2, "assistant", "because")]));
    inProject(<MessageThread sessionId={1} />);
    expect(await screen.findByText("because")).toBeInTheDocument();
    expect(await screen.findByTestId("chat-budget-notice")).toHaveTextContent(
      "This project's monthly LLM budget is reached",
    );
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("keeps the composer when nothing blocks", async () => {
    fake.routes["/api/projects/alpha/chat/sessions/1"] = () => json(transcript([]));
    inProject(<MessageThread sessionId={1} />);
    expect(await screen.findByRole("textbox")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-budget-notice")).toBeNull();
  });

  it("turns a persisted budget-stop row into a notice, not a rose error", () => {
    const turns = turnsFromMessages([
      msg(1, "user", "why?") as never,
      msg(2, "assistant", "partial", { input_tokens: 5, output_tokens: 2 }) as never,
      msg(3, "assistant", "This chat has reached its monthly budget.", { error: "budget_exceeded" }) as never,
    ]);
    const assistant = turns[1];
    expect(assistant.kind).toBe("assistant");
    if (assistant.kind !== "assistant") return;
    expect(assistant.error).toBeUndefined();
    expect(assistant.budgetStop?.message).toBe("This chat has reached its monthly budget.");
    expect(assistant.segments.join("")).toBe("partial");
  });

  it("renders the persisted row as a notice inside the thread", async () => {
    fake.routes["/api/projects/alpha/chat/sessions/1"] = () =>
      json(
        transcript([
          msg(1, "user", "why?"),
          msg(2, "assistant", "This chat has reached its monthly budget.", { error: "budget_exceeded" }),
        ]),
      );
    inProject(<MessageThread sessionId={1} />);
    expect(await screen.findByTestId("budget-stop-row")).toHaveTextContent("This chat has reached its monthly budget.");
    expect(screen.queryByText("Thinking…")).toBeNull();
  });

  it("a live budget_exceeded frame shows the notice and swaps the composer", async () => {
    fake.routes["/api/projects/alpha/chat/sessions/1"] = () => json(transcript([]));
    fake.routes["POST /api/projects/alpha/chat/sessions/1/messages"] = () =>
      sse([
        { type: "text_delta", text: "Looking" },
        { type: "budget_exceeded", scope: "member", message_id: 9, message: "This chat has reached its monthly budget." },
        { type: "done", message_id: 9, input_tokens: 1, output_tokens: 1, finish_reason: null },
      ]);
    const user = userEvent.setup();
    inProject(<MessageThread sessionId={1} />);
    await user.type(await screen.findByRole("textbox"), "hello");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByTestId("chat-budget-notice")).toHaveTextContent(MEMBER_STOPPED_LINE);
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("a 403 budget_exceeded from send shows the same notice and no error", async () => {
    fake.routes["/api/projects/alpha/chat/sessions/1"] = () => json(transcript([]));
    fake.routes["POST /api/projects/alpha/chat/sessions/1/messages"] = () =>
      json({ detail: "the monthly LLM budget of this project is reached", code: "budget_exceeded", scope: "project" }, 403);
    const user = userEvent.setup();
    inProject(<MessageThread sessionId={1} />);
    await user.type(await screen.findByRole("textbox"), "hello");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByTestId("chat-budget-notice")).toHaveTextContent("This project's monthly LLM budget");
    expect(screen.queryByText(/the monthly LLM budget of this project is reached/)).toBeNull();
  });
});

describe("authMessage for the M2f-2 refusals", () => {
  const err = (status: number, code: string, extra: Record<string, unknown>) => new ApiError(status, "x", code, extra);
  it("names the scope of a budget refusal", () => {
    expect(authMessage(err(403, "budget_exceeded", { scope: "org" }))).toBe(budgetNoticeText("org"));
    expect(authMessage(err(403, "budget_exceeded", { scope: "member" }))).toBe(MEMBER_STOPPED_LINE);
    expect(authMessage(err(403, "budget_exceeded", { scope: "project" }))).toContain("This project's");
  });
  it("tells the org limit from the member limit", () => {
    expect(authMessage(err(429, "generation_limited", { scope: "member" }))).toContain("your hourly limit");
    expect(authMessage(err(429, "generation_limited", { scope: "org" }))).toContain("This organization");
    expect(authMessage(err(403, "generation_disabled", { scope: "member" }))).toContain("your account");
    expect(authMessage(err(403, "generation_disabled", { scope: "org" }))).toContain("this organization");
  });
});

// ---- Members, Access, project Usage, Account ----------------------------------------

describe("This month on the Members page", () => {
  it("shows a column linking to the drill-down when rows carry the spend", async () => {
    fake.state = orgState("admin", usageBlock(null, gauge()));
    fake.routes["/api/org/members"] = () =>
      json([
        { ...MEMBERS[0], month_spend_usd: 4.2, month_split: { interactive: 1, scans: 2 } },
        { ...MEMBERS[1], month_spend_usd: 0 },
      ]);
    mount("/members");
    const link = await screen.findByTestId("member-spend-u1");
    expect(link).toHaveTextContent("$4.20");
    expect(link).toHaveAttribute("href", "/usage/members/u1");
    expect(screen.getByTestId("member-spend-u2")).toHaveTextContent("$0.00");
  });

  it("shows no column when the payload has no spend", async () => {
    fake.state = orgState("member", usageBlock(null, null));
    mount("/members");
    await screen.findByTestId("member-u1");
    expect(screen.queryByTestId("member-spend-u1")).toBeNull();
  });
});

describe("Access spend", () => {
  const person = (uid: string, name: string, extra: Record<string, unknown> = {}) => ({
    uid,
    login: name.toLowerCase(),
    name,
    avatar: null,
    org_role: "member",
    project_role: "contributor",
    source: "grant",
    ...extra,
  });
  it("shows each person's month spend on the project only when present", async () => {
    fake.routes["/api/projects/alpha/access"] = () =>
      json({
        restricted: true,
        org_default: "viewer",
        people: [person("u1", "Ada", { month_spend_usd: 3.5 }), person("u2", "Ben")],
        invitations: [],
      });
    inProject(<ProjectAccess slug="alpha" />);
    expect(await screen.findByTestId("access-spend-u1")).toHaveTextContent("$3.50 this month");
    expect(screen.queryByTestId("access-spend-u2")).toBeNull();
  });
});

describe("project Usage section", () => {
  const report = {
    range: { from: "2026-10-01", to: "2026-11-01" },
    totals: {
      calls: 7,
      input_tokens: 7000,
      output_tokens: 700,
      cache_read_tokens: null,
      cache_write_tokens: null,
      reasoning_tokens: null,
      cost_usd: 3.25,
      unpriced_calls: 0,
    },
    split: { interactive: { calls: 5, cost_usd: 2 }, scans: { calls: 2, cost_usd: 1.25 } },
    series: [{ day: "2026-10-01", calls: 7, cost_usd: 3.25, input_tokens: 7000, output_tokens: 700 }],
    members: [
      {
        key: "u2",
        label: "Ben (@ben)",
        calls: 7,
        input_tokens: 7000,
        output_tokens: 700,
        cache_read_tokens: null,
        cache_write_tokens: null,
        reasoning_tokens: null,
        cost_usd: 3.25,
        unpriced_calls: 0,
        interactive: { calls: 5, cost_usd: 2 },
        scans: { calls: 2, cost_usd: 1.25 },
        top_project: null,
      },
    ],
  };
  const withUsage = project("alpha", {
    usage: { month_spend_usd: 3.25, budget: { monthly_usd: 10, hard_stop: true }, pct: 32.5 },
  });

  it("shows totals, the per-member list and the project budget editor to an org admin", async () => {
    fake.state = orgState("admin", usageBlock(null, gauge()));
    fake.routes["/api/projects/alpha/usage"] = () => json(report);
    inProject(<ProjectUsageSection slug="alpha" project={withUsage as never} />);
    expect(await screen.findByTestId("project-usage-spend")).toHaveTextContent("$3.25");
    expect(screen.getByTestId("project-usage-budget")).toHaveTextContent("32.5%");
    expect(screen.getByTestId("project-usage-members")).toHaveTextContent("Ben (@ben)");
    expect(await screen.findByTestId("project-budget")).toBeInTheDocument();
  });

  it("leaves the budget editor out for someone who cannot edit budgets", async () => {
    fake.state = orgState("member", usageBlock(null, null));
    fake.routes["/api/projects/alpha/usage"] = () => json({ ...report, members: [] });
    inProject(<ProjectUsageSection slug="alpha" project={withUsage as never} />);
    await screen.findByTestId("project-usage-spend");
    expect(screen.queryByTestId("project-budget")).toBeNull();
    expect(screen.queryByTestId("project-usage-members")).toBeNull();
  });

  it("is a settings section only with project.usage", async () => {
    fake.state = orgState("admin", usageBlock(null, gauge()));
    fake.routes["/api/projects/alpha"] = () => json(withUsage);
    fake.routes["/api/projects/alpha/usage"] = () => json(report);
    fake.routes["/api/projects/alpha/access"] = () =>
      json({ restricted: false, org_default: "viewer", people: [], invitations: [] });
    mount("/p/alpha/settings");
    const nav = await screen.findByRole("navigation", { name: "Settings sections" });
    expect(within(nav).getByRole("button", { name: "Usage" })).toBeInTheDocument();
    expect(await screen.findByTestId("project-usage")).toBeInTheDocument();
  });

  it("is absent without project.usage", async () => {
    fake.state = orgState("member", usageBlock(null, null));
    fake.routes["/api/projects/alpha"] = () =>
      json(project("alpha", { permissions: PROJECT_ACTIONS.filter((a) => a !== "project.usage") }));
    mount("/p/alpha/settings");
    const nav = await screen.findByRole("navigation", { name: "Settings sections" });
    expect(within(nav).queryByRole("button", { name: "Usage" })).toBeNull();
    expect(screen.queryByTestId("project-usage")).toBeNull();
  });
});

describe("Account: My usage", () => {
  const baseState = {
    mode: "production",
    host_kind: "base",
    base_url: BASE,
    setup_complete: true,
    bootstrap_required: false,
    user: ada,
    org: null,
  };
  it("lists this month per org with a budget bar and a link to its My usage page", async () => {
    fake.state = baseState;
    fake.routes["/api/account"] = () =>
      json({ ...ada, display_name: "Ada", has_password: false, avatar_url: null, github_login: "ada" });
    fake.routes["/api/account/usage"] = () =>
      json({
        month: "2026-10",
        resets_at: "2026-11-01T00:00:00+00:00",
        orgs: [
          { slug: "acme", name: "Acme", url: "https://acme.whygraph.example", spent_usd: 12.5, calls: 30, budget_usd: 50, pct: 25, hard_stop: true },
          { slug: "solo", name: "Solo", url: "https://solo.whygraph.example", spent_usd: 0, calls: 0, budget_usd: null, pct: null, hard_stop: false },
        ],
      });
    mount("/account");
    const acme = await screen.findByTestId("account-usage-acme");
    expect(acme).toHaveTextContent("$13 of $50");
    expect(within(acme).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
    expect(within(acme).getByRole("link", { name: "Open my usage" })).toHaveAttribute(
      "href",
      "https://acme.whygraph.example/usage/me",
    );
    expect(within(screen.getByTestId("account-usage-solo")).queryByRole("progressbar")).toBeNull();
  });
});
