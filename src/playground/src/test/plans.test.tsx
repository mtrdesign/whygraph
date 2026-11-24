import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PlanOutlineEntry, PlanRecord, PlanRef } from "../api";
import { layerToValues, valuesToLayer } from "../lib/configForm";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { validatePlansSearch } from "../lib/routeSearch";
import { createAppRouter } from "../router";
import { useUi } from "../store";
import { ThemeProvider } from "../theme";

interface Fake {
  plans: PlanRef[];
  dir: string;
  source: string;
  record?: Partial<PlanRecord>;
  listError?: boolean;
}

let fake: Fake;
let calls: string[];

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const ref = (n: number, over: Partial<PlanRef> = {}): PlanRef => ({
  stem: `plan-${n}`,
  title: `Plan ${n}`,
  status: "shipped",
  uri: `whygraph://plan/plan-${n}`,
  path: `docs/plans/plan-${n}.md`,
  present: true,
  objective: `Objective ${n}`,
  updated_at: "2026-11-01T00:00:00Z",
  ...over,
});

const entry = (n: number, title: string, over: Partial<PlanOutlineEntry> = {}): PlanOutlineEntry => ({
  n,
  level: 2,
  title,
  part: null,
  step: null,
  ...over,
});

const BODY = [
  "# Plan x",
  "",
  "intro",
  "",
  "## Goals",
  "",
  "text",
  "",
  "```",
  "## not a heading",
  "```",
  "",
  "## Steps",
  "",
  "1. **First** do a",
  "2. **Second** do b",
  "",
  "## Risks",
  "",
  "risk text",
].join("\n");

const OUTLINE: PlanOutlineEntry[] = [
  entry(0, "Header", { level: 1 }),
  entry(1, "Goals"),
  entry(2, "Steps"),
  entry(3, "Step 1: First", { level: 3, step: 1 }),
  entry(4, "Step 2: Second", { level: 3, step: 2 }),
  entry(5, "Risks"),
  entry(6, "Risks (continued 2/2)", { part: [2, 2] }),
];
// the first Risks part carries part [1, 2]
OUTLINE[5] = entry(5, "Risks", { part: [1, 2] });

function record(stem: string): PlanRecord {
  return {
    ...ref(1, { stem, title: "Plan x" }),
    kind: "plan",
    path: "docs/plans/x.md",
    present: true,
    objective: null,
    line_count: 19,
    redacted_lines: 0,
    updated_at: "2026-11-01T00:00:00Z",
    publishing_links: [],
    implementing_commits: [{ sha: "abcd1234", subject: "Build it", on_default_branch: true }],
    implementing_commits_total: 1,
    pull_requests: [{ number: 7, title: "The PR", state: "merged" }],
    pull_requests_total: 1,
    paths: ["src/a.py"],
    paths_total: 1,
    body: BODY + "\n\n![pic](https://evil.example/x.png)",
    truncated: false,
    digest: "- a decision",
    outline: OUTLINE,
    ...fake.record,
  } as PlanRecord;
}

function details(slug: string) {
  return {
    slug,
    name: `Project ${slug}`,
    source: fake.source,
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

function fakeFetch(input: RequestInfo | URL): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  calls.push(url.pathname + url.search);
  const path = url.pathname;
  if (path === "/api/portal/state")
    return Promise.resolve(
      json({
        mode: "local",
        setup_complete: true,
        user: { uid: "u1", display_name: "Ada", role: "owner" },
        port: 8765,
        shared_folders: [],
        usage: null,
      }),
    );
  if (path === "/api/projects") return Promise.resolve(json({ projects: [details("alpha")] }));
  const m = /^\/api\/projects\/alpha(\/.*)?$/.exec(path);
  if (!m) return Promise.resolve(json({ error: "unhandled" }, 500));
  const rest = m[1];
  if (!rest) return Promise.resolve(json(details("alpha")));
  if (rest === "/chat/sessions") return Promise.resolve(json([]));
  if (rest === "/notes/summary") return Promise.resolve(json({ proposed: 0, proposed_by_agents: 0, confirmed: 0 }));
  if (rest === "/plans") {
    if (fake.listError) return Promise.resolve(json({ error: "boom" }, 500));
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    const status = url.searchParams.get("status");
    const limit = Number(url.searchParams.get("limit") ?? 50);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    const rows = fake.plans.filter((p) => (!status || p.status === status) && p.title.toLowerCase().includes(q));
    return Promise.resolve(json({ plans: rows.slice(offset, offset + limit), total: rows.length, dir: fake.dir }));
  }
  const one = /^\/plans\/([^/]+)$/.exec(rest);
  if (one) {
    if (one[1] === "missing") return Promise.resolve(json({ error: "gone", code: "plan_not_found" }, 404));
    return Promise.resolve(json(record(one[1])));
  }
  return Promise.resolve(json({ error: "unhandled" }, 500));
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
  router.state.location.pathname + (router.state.location.searchStr ?? "");

let scrolled: Element[];

beforeEach(() => {
  fake = { plans: [ref(1), ref(2, { status: "in_flight" })], dir: "docs/plans", source: "local" };
  calls = [];
  scrolled = [];
  window.localStorage.clear();
  useUi.setState({ paletteOpen: false, navOpen: false });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  Element.prototype.scrollIntoView = function (this: Element) {
    if (this.closest('[data-testid="plan-body"]')) scrolled.push(this);
  };
});
afterEach(() => vi.unstubAllGlobals());

describe("validatePlansSearch", () => {
  it("returns all four keys every time", () => {
    expect(validatePlansSearch({})).toEqual({ status: undefined, q: undefined, stem: undefined, section: undefined });
    expect(Object.keys(validatePlansSearch({ junk: 1 })).sort()).toEqual(["q", "section", "status", "stem"]);
  });

  it("drops an unknown status, an over-long q and a bad stem", () => {
    const v = validatePlansSearch({ status: "done", q: "x".repeat(201), stem: "Bad Stem", section: "3" });
    expect(v).toEqual({ status: undefined, q: undefined, stem: undefined, section: undefined });
  });

  it("drops a bad section, and a section without a stem", () => {
    for (const section of [-1, 300, 1.5, "abc", "-2", "1.5"]) {
      expect(validatePlansSearch({ stem: "x", section }).section).toBeUndefined();
    }
    expect(validatePlansSearch({ section: "3" }).section).toBeUndefined();
  });

  it("keeps valid values, section 0 included", () => {
    expect(validatePlansSearch({ status: "in_flight", q: " hello ", stem: "my-plan", section: "0" })).toEqual({
      status: "in_flight",
      q: "hello",
      stem: "my-plan",
      section: 0,
    });
    expect(validatePlansSearch({ stem: "x", section: 299 }).section).toBe(299);
  });
});

describe("PlansPage", () => {
  it("renders the list with status and path", async () => {
    mount("/p/alpha/plans");
    const list = await screen.findByTestId("plans-list");
    expect(within(list).getByText("Plan 1")).toBeInTheDocument();
    expect(within(list).getByText("docs/plans/plan-2.md")).toBeInTheDocument();
    expect(within(list).getByText("In flight")).toBeInTheDocument();
  });

  it("a status tab and the search write the URL with replace, and Clear filters resets them", async () => {
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/plans");
    await screen.findByTestId("plans-list");
    const before = router.history.length;
    await user.click(screen.getByTestId("status-in_flight"));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/plans?status=in_flight"));
    await waitFor(() => expect(screen.queryByText("Plan 1")).toBeNull());
    await user.type(screen.getByTestId("plans-search"), "Plan 2");
    await waitFor(() => expect(here(router)).toBe("/p/alpha/plans?status=in_flight&q=Plan+2"), { timeout: 2000 });
    expect(router.history.length).toBe(before);
    await user.click(screen.getByTestId("clear-filters"));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/plans"));
    await waitFor(() => expect(screen.getByTestId("plans-search")).toHaveValue(""));
    expect(await screen.findByText("Plan 1")).toBeInTheDocument();
  });

  it("pages by offset", async () => {
    fake.plans = Array.from({ length: 60 }, (_, i) => ref(i + 1));
    const user = userEvent.setup();
    mount("/p/alpha/plans");
    await screen.findByTestId("plans-list");
    expect(within(screen.getByTestId("plans-list")).getAllByRole("listitem")).toHaveLength(50);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(within(screen.getByTestId("plans-list")).getAllByRole("listitem")).toHaveLength(60));
    expect(calls.some((c) => c.includes("offset=50"))).toBe(true);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("?stem= shows the detail with rendered Markdown and no image", async () => {
    mount("/p/alpha/plans?stem=x");
    const detail = await screen.findByTestId("plan-detail");
    expect(within(detail).getByRole("heading", { name: "Goals" })).toBeInTheDocument();
    expect(detail.querySelector("img")).toBeNull();
    expect(within(detail).getByText("abcd1234")).toBeInTheDocument();
    expect(within(detail).getByText(/#7 The PR/)).toBeInTheDocument();
    expect(within(detail).getByText("src/a.py")).toBeInTheDocument();
  });

  it("shows the truncation and redaction notes", async () => {
    fake.record = { truncated: true, redacted_lines: 2 };
    mount("/p/alpha/plans?stem=x");
    expect(await screen.findByText("Truncated at 512 KiB")).toBeInTheDocument();
    expect(screen.getByText(/2 lines were removed by secret redaction/)).toBeInTheDocument();
  });

  it("an unknown stem shows the plan_not_found message inline", async () => {
    mount("/p/alpha/plans?stem=missing");
    expect(await screen.findByTestId("error-state")).toBeInTheDocument();
  });

  it("the three empty states", async () => {
    fake.plans = [];
    fake.dir = "";
    const first = mount("/p/alpha/plans");
    expect(await screen.findByText("Plan indexing is off")).toBeInTheDocument();
    expect(screen.getByText("Set a plans folder in Settings, Plans.")).toBeInTheDocument();
    void first;
  });

  it("empty with indexing on names the folder and the next step", async () => {
    fake.plans = [];
    mount("/p/alpha/plans");
    expect(await screen.findByText("No plans yet")).toBeInTheDocument();
    expect(screen.getByText(/No plans in docs\/plans yet\. Publish one with/)).toBeInTheDocument();
  });

  it("empty under a filter says no matches", async () => {
    fake.plans = [];
    mount("/p/alpha/plans?q=zzz");
    expect(await screen.findByText("No matches")).toBeInTheDocument();
    expect(screen.getByText("No plans match these filters.")).toBeInTheDocument();
  });

  it("an error shows ErrorState with Retry", async () => {
    fake.listError = true;
    const user = userEvent.setup();
    mount("/p/alpha/plans");
    expect(await screen.findByText("Couldn't load the plans")).toBeInTheDocument();
    fake.listError = false;
    await user.click(screen.getByRole("button", { name: /Retry/ }));
    expect(await screen.findByTestId("plans-list")).toBeInTheDocument();
  });
});

describe("PlansPage section deep link", () => {
  it("scrolls to the k-th counted heading; a heading inside a code fence is not counted", async () => {
    mount("/p/alpha/plans?stem=x&section=5");
    await screen.findByTestId("plan-detail");
    // Counted headings: Goals, Steps, Risks (the fenced `## not a heading` is not a heading).
    await waitFor(() => expect(scrolled).toHaveLength(1));
    expect(scrolled[0].tagName).toBe("H2");
    expect(scrolled[0].textContent).toBe("Risks");
  });

  it("a continuation part opens at its first part", async () => {
    mount("/p/alpha/plans?stem=x&section=6");
    await screen.findByTestId("plan-detail");
    await waitFor(() => expect(scrolled).toHaveLength(1));
    expect(scrolled[0].textContent).toBe("Risks");
  });

  it("a step entry scrolls to the numbered step under ## Steps", async () => {
    mount("/p/alpha/plans?stem=x&section=4");
    await screen.findByTestId("plan-detail");
    await waitFor(() => expect(scrolled).toHaveLength(1));
    expect(scrolled[0].tagName).toBe("LI");
    expect(scrolled[0].textContent).toContain("Second");
  });

  it("a number that matches nothing leaves the page at the top", async () => {
    mount("/p/alpha/plans?stem=x&section=99");
    await screen.findByTestId("plan-detail");
    expect(scrolled).toHaveLength(0);
  });
});

describe("Plans nav", () => {
  it("shows Plans for a local project, linking to the page", async () => {
    mount("/p/alpha");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    const link = await within(nav).findByRole("link", { name: /Plans/ });
    expect(link).toHaveAttribute("href", "/p/alpha/plans");
  });

  it("hides Plans for a linked project", async () => {
    fake.source = "platform";
    mount("/p/alpha");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    await within(nav).findByRole("link", { name: "Overview" });
    expect(within(nav).queryByRole("link", { name: /Plans/ })).toBeNull();
  });

  it("the command palette lists Plans for a local project only", async () => {
    const user = userEvent.setup();
    mount("/p/alpha");
    await screen.findByRole("navigation", { name: "Main" });
    useUi.setState({ paletteOpen: true });
    await user.type(await screen.findByRole("combobox"), "plans");
    expect(await screen.findByRole("option", { name: /Plans/ })).toBeInTheDocument();
  });
});

describe("Plans in the palette for a linked project", () => {
  it("is absent", async () => {
    fake.source = "platform";
    const user = userEvent.setup();
    mount("/p/alpha");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    await within(nav).findByRole("link", { name: "Overview" });
    useUi.setState({ paletteOpen: true });
    await user.type(await screen.findByRole("combobox"), "plans");
    await screen.findByText(/No results|No matches|Nothing/i).catch(() => undefined);
    expect(screen.queryByRole("option", { name: /Plans/ })).toBeNull();
  });
});

describe("[plans].dir in the config form", () => {
  it("round-trips the three states", () => {
    expect(layerToValues({})).toMatchObject({ plansDir: "", plansOff: false });
    expect(layerToValues({ plans: { dir: "" } })).toMatchObject({ plansDir: "", plansOff: true });
    expect(layerToValues({ plans: { dir: "docs/p" } })).toMatchObject({ plansDir: "docs/p", plansOff: false });

    for (const layer of [{}, { plans: { dir: "" } }, { plans: { dir: "docs/p" } }]) {
      expect(valuesToLayer(layer, layerToValues(layer), { scan: true, plans: true })).toEqual(layer);
    }
    const off = { ...layerToValues({}), plansOff: true };
    expect(valuesToLayer({}, off, { scan: true, plans: true })).toEqual({ plans: { dir: "" } });
    const dir = { ...layerToValues({}), plansDir: " docs/x " };
    expect(valuesToLayer({}, dir, { scan: true, plans: true })).toEqual({ plans: { dir: "docs/x" } });
    // Another section's save never writes [plans].
    expect(valuesToLayer({}, off, { scan: true })).toEqual({});
  });
});
