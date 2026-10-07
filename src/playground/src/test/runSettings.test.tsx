import { render, screen, waitFor, within } from "@testing-library/react";
import { PROJECT_ACTIONS } from "../lib/permissions";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

// Step 13's screens against a fake portal: the scan run (live + finished), history,
// the project overview, project and global settings, removal, and the edge states.
// A handler table keyed `METHOD /path` stands in for the backend.

type Json = Record<string, unknown>;
type Reply = { status: number; body: unknown } | unknown;
type Handler = (body: Json | null, url: URL) => Reply;

let handlers: Record<string, Handler>;
let log: { method: string; path: string; body: Json | null }[];

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function sse(frames: string[]) {
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
}

const frame = (id: number, event: Json) => `id: ${id}\ndata: ${JSON.stringify(event)}\n\n`;
const endFrame = (id: number, status: string, summary: Json | null = null, run = 6) =>
  `id: ${id}\nevent: end\ndata: ${JSON.stringify({ type: "end", run_id: run, status, summary })}\n\n`;

const noKey = { set: false, hint: null };
const emptySecrets = () => ({
  llm: { anthropic: noKey, openai: noKey, deepseek: noKey, openrouter: noKey },
  github_token: noKey,
});

function summary(slug: string, over: Json = {}): Json {
  return {
    slug,
    name: slug[0].toUpperCase() + slug.slice(1),
    source: "local",
    root: `/repos/${slug}`,
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
    last_scan_status: "ok",
    stale: null,
    ...over,
  };
}

function details(slug: string, over: Json = {}): Json {
  return {
    ...summary(slug),
    agents: ["claude"],
    missing_key: null,
    mcp_url: `http://127.0.0.1:8765/mcp/${slug}`,
    detected: { existing_db: false, managed_hooks: [], detected_agents: [], custom_db_paths: [] },
    stats: {
      commits: 240,
      described: 178,
      described_pct: 74.2,
      pull_requests: 50,
      issues: 12,
      rationale_cards: 31,
    },
    ...over,
  };
}

function run(id: number, over: Json = {}): Json {
  return {
    id,
    kind: "scan",
    trigger: "manual",
    analyze: true,
    status: "ok",
    requested_by: 1,
    started_at: "2026-09-30T11:00:00+00:00",
    finished_at: "2026-09-30T11:01:08+00:00",
    summary: { status: "ok", elapsed_sec: 68.2 },
    ...over,
  };
}

const PHASE_EVENTS = [
  frame(1, { type: "start", phase_total: 3 }),
  frame(2, { type: "phase", phase: 1, title: "Structural crawl" }),
  frame(3, { type: "task", name: "git", completed: 240, total: 240, description: "240 commits" }),
  frame(4, { type: "phase", phase: 2, title: "Author identity" }),
  frame(5, { type: "phase", phase: 3, title: "LLM descriptions" }),
  frame(6, { type: "task", name: "analyze", completed: 41, total: 62, description: "describing" }),
];

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const method = init?.method ?? "GET";
  const body = init?.body ? (JSON.parse(String(init.body)) as Json) : null;
  log.push({ method, path: url.pathname + url.search, body });
  const handler = handlers[`${method} ${url.pathname}`];
  if (!handler) return Promise.resolve(json({ error: `unhandled ${method} ${url.pathname}` }, 500));
  const out = handler(body, url);
  if (out instanceof Response) return Promise.resolve(out);
  if (out && typeof out === "object" && "status" in out && "body" in out) {
    const r = out as { status: number; body: unknown };
    return Promise.resolve(json(r.body, r.status));
  }
  return Promise.resolve(json(out));
}

const calls = (method: string, path: string) => log.filter((c) => c.method === method && c.path === path);

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
  return { router };
}

const here = (router: ReturnType<typeof mount>["router"]) => router.state.location.pathname;

beforeEach(() => {
  log = [];
  handlers = {
    "GET /api/portal/state": () => ({
      mode: "local",
      setup_complete: true,
      user: { uid: "u1", display_name: "Ada", role: "owner" },
      port: 8765,
      shared_folders: ["/repos"],
      version: "2.0.0",
    }),
    "GET /api/projects": () => ({ projects: [summary("alpha")] }),
    "GET /api/projects/alpha": () => details("alpha"),
    "GET /api/projects/alpha/scans": () => ({ runs: [run(6, { status: "running", finished_at: null, summary: null })] }),
    "GET /api/projects/alpha/scans/6/events": () => sse(PHASE_EVENTS),
    "GET /api/projects/alpha/scans/6/log": () => ({ run_id: 6, text: "phase 1/3\nall good\n", size: 20, truncated: false }),
    "GET /api/projects/alpha/scan-estimate": () => ({
      commits: 10,
      upper_bound: true,
      large_commits: 0,
      model: { provider: "anthropic", model: "claude-haiku-4-5" },
      tokens: { input: 1, output: 1, input_range: { low: 1, high: 1 }, output_range: { low: 1, high: 1 } },
      cost: null,
      missing_key: null,
    }),
  };
  window.localStorage.clear();
  window.sessionStorage.clear();
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

// ---- screen 7: the scan run -----------------------------------------------------------------

/** Rescan is a menu for a project admin: open it, pick the quick one. */
async function quickRescan(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getAllByRole("button", { name: "Rescan" })[0]);
  await user.click(await screen.findByRole("menuitem", { name: "Quick rescan" }));
}

describe("Scan run (screen 7)", () => {
  it("shows phases live from the stream, a bar for the LLM phase, and keeps it on the same run", async () => {
    mount("/p/alpha/scans/6");
    const phase3 = await screen.findByTestId("phase-3");
    expect(phase3).toHaveAttribute("data-status", "running");
    expect(screen.getByTestId("phase-1")).toHaveAttribute("data-status", "done");
    expect(screen.getByTestId("phase-2")).toHaveAttribute("data-status", "done");
    // One bar with its counter for the LLM descriptions task.
    const task = within(phase3).getByTestId("task-analyze");
    expect(task).toHaveTextContent("41 / 62");
    expect(within(phase3).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "66");
    expect(screen.getByRole("heading", { name: /Scan #6/ })).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    // The stream is a `fetch` (never EventSource) that carries the client header.
    expect(calls("GET", "/api/projects/alpha/scans/6/events")).toHaveLength(1);
  });

  it("a finished run shows timings, the crawler summaries and the outcome", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({ runs: [run(6)] });
    handlers["GET /api/projects/alpha/scans/6/events"] = () =>
      sse([
        frame(1, { type: "start", phase_total: 2 }),
        frame(2, { type: "phase", phase: 1, title: "Structural crawl" }),
        frame(3, { type: "phase", phase: 2, title: "Author identity" }),
        frame(
          4,
          {
            type: "result",
            status: "ok",
            elapsed_sec: 68.2,
            phase_timings: { "Structural crawl": 41.2, "Author identity": 0.04 },
            crawlers: [{ name: "git", status: "ok", summary: "240 commits (3 new)" }],
            analyze_skipped: null,
          },
        ),
        endFrame(5, "ok", { elapsed_sec: 68.2 }),
      ]);
    mount("/p/alpha/scans/6");
    const result = await screen.findByTestId("run-result");
    expect(result).toHaveTextContent("Finished in 1m 08s");
    expect(screen.getByTestId("phase-1")).toHaveTextContent("41s");
    expect(screen.getByTestId("phase-1")).toHaveTextContent("240 commits (3 new)");
    expect(screen.getByText("Succeeded")).toBeInTheDocument();
    // A good run keeps the log collapsed and does not fetch it.
    expect(calls("GET", "/api/projects/alpha/scans/6/log")).toHaveLength(0);
  });

  it("a failed run shows the error and opens the log excerpt", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({
      runs: [run(6, { status: "failed", summary: { error: "codegraph crashed", exit_code: 1 } })],
    });
    handlers["GET /api/projects/alpha/scans/6/events"] = () =>
      sse([
        frame(1, { type: "start", phase_total: 2 }),
        frame(2, { type: "phase", phase: 1, title: "Structural crawl" }),
        endFrame(3, "failed", { error: "codegraph crashed", exit_code: 1 }),
      ]);
    handlers["GET /api/projects/alpha/scans/6/log"] = () => ({
      run_id: 6,
      text: "Traceback\nboom",
      size: 90_000,
      truncated: true,
    });
    mount("/p/alpha/scans/6");
    const result = await screen.findByTestId("run-result");
    expect(result).toHaveTextContent("The scan failed");
    expect(result).toHaveTextContent("codegraph crashed");
    // The failing phase is marked, and the log is fetched and shown without a click.
    expect(screen.getByTestId("phase-1")).toHaveAttribute("data-status", "failed");
    const log = await screen.findByTestId("run-log");
    await waitFor(() => expect(log).toHaveTextContent("Traceback"));
    expect(log).toHaveTextContent("Showing the end of the log");
  });

  it("Cancel follows the run: a contributor stops a structure-only run, not one that may spend", async () => {
    const contributor = ["project.read", "project.chat", "project.scan"];
    handlers["GET /api/projects/alpha"] = () => details("alpha", { my_role: "contributor", permissions: contributor });
    handlers["GET /api/projects/alpha/scans"] = () => ({
      runs: [run(6, { status: "running", analyze: true, finished_at: null, summary: null })],
    });
    mount("/p/alpha/scans/6");
    await screen.findByTestId("phase-3");
    expect(screen.queryByTestId("cancel-run")).toBeNull();
  });

  it("a contributor sees Cancel on a structure-only run", async () => {
    const contributor = ["project.read", "project.chat", "project.scan"];
    handlers["GET /api/projects/alpha"] = () => details("alpha", { my_role: "contributor", permissions: contributor });
    handlers["GET /api/projects/alpha/scans"] = () => ({
      runs: [run(6, { status: "running", analyze: false, finished_at: null, summary: null })],
    });
    mount("/p/alpha/scans/6");
    await screen.findByTestId("phase-3");
    expect(await screen.findByTestId("cancel-run")).toBeInTheDocument();
  });

  it("a queued run says it is waiting", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({
      runs: [run(6, { status: "queued", started_at: null, finished_at: null, summary: null })],
    });
    handlers["GET /api/projects/alpha/scans/6/events"] = () => sse([]);
    mount("/p/alpha/scans/6");
    await waitFor(() => expect(screen.getByTestId("run-waiting")).toHaveTextContent("Waiting for its turn"));
  });

  it("Cancel on a running run asks first, then POSTs the cancel", async () => {
    handlers["POST /api/projects/alpha/scans/6/cancel"] = () => ({
      status: 202,
      body: { run_id: 6, was: "running" },
    });
    const user = userEvent.setup();
    mount("/p/alpha/scans/6");
    await screen.findByTestId("phase-3");

    await user.click(screen.getByTestId("cancel-run"));
    const dialog = await screen.findByTestId("cancel-run-dialog");
    expect(dialog).toHaveTextContent("Cancel scan #6?");
    expect(dialog).toHaveTextContent("Commits it already described are kept");
    await user.click(within(dialog).getByRole("button", { name: "Keep it" }));
    await waitFor(() => expect(screen.queryByTestId("cancel-run-dialog")).not.toBeInTheDocument());
    expect(calls("POST", "/api/projects/alpha/scans/6/cancel")).toHaveLength(0);

    await user.click(screen.getByTestId("cancel-run"));
    await user.click(
      within(await screen.findByTestId("cancel-run-dialog")).getByRole("button", { name: "Cancel scan" }),
    );
    await waitFor(() => expect(calls("POST", "/api/projects/alpha/scans/6/cancel")).toHaveLength(1));
  });

  it("a queued run can be cancelled too; the dialog says it just leaves the queue", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({
      runs: [run(6, { status: "queued", started_at: null, finished_at: null, summary: null })],
    });
    handlers["GET /api/projects/alpha/scans/6/events"] = () => sse([]);
    const user = userEvent.setup();
    mount("/p/alpha/scans/6");
    await user.click(await screen.findByTestId("cancel-run"));
    expect(await screen.findByTestId("cancel-run-dialog")).toHaveTextContent("removed from the queue");
  });

  it("a run the user cancelled says so, and offers no Cancel", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({
      runs: [run(6, { status: "cancelled", summary: { cancelled_by: "user" } })],
    });
    handlers["GET /api/projects/alpha/scans/6/events"] = () =>
      sse([
        frame(1, { type: "start", phase_total: 2 }),
        endFrame(2, "cancelled", { cancelled_by: "user" }),
      ]);
    mount("/p/alpha/scans/6");
    const result = await screen.findByTestId("run-result");
    expect(result).toHaveTextContent("Cancelled");
    expect(result).toHaveTextContent("You cancelled this run");
    expect(screen.queryByTestId("cancel-run")).not.toBeInTheDocument();
  });

  it("an unknown run id is reported, not retried forever", async () => {
    handlers["GET /api/projects/alpha/scans/99/events"] = () => ({
      status: 404,
      body: { error: "run 99 not found", code: "not_found" },
    });
    mount("/p/alpha/scans/99");
    expect(await screen.findByTestId("run-unavailable")).toHaveTextContent("Run #99 was not found");
    expect(calls("GET", "/api/projects/alpha/scans/99/events")).toHaveLength(1);
  });

  it("Scan now during a run queues one follow-up; asking again folds into the same run id", async () => {
    // Backend behaviour: a request while a run is active joins the ONE pending run.
    handlers["POST /api/projects/alpha/scans"] = () => ({ status: 202, body: { run_id: 7 } });
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/scans/6");
    await screen.findByTestId("phase-3");

    await quickRescan(user);
    const followup = await screen.findByTestId("followup");
    expect(followup).toHaveTextContent("run #7");
    // The live view of the running run is not abandoned.
    expect(here(router)).toBe("/p/alpha/scans/6");

    await quickRescan(user);
    await waitFor(() => expect(calls("POST", "/api/projects/alpha/scans")).toHaveLength(2));
    // Same id both times: still exactly one follow-up notice, still on run 6.
    expect(screen.getAllByTestId("followup")).toHaveLength(1);
    expect(here(router)).toBe("/p/alpha/scans/6");
    expect(calls("POST", "/api/projects/alpha/scans").map((c) => c.body)).toEqual([
      { trigger: "manual", analyze: false },
      { trigger: "manual", analyze: false },
    ]);
  });

  it("Scan again after a finished run opens the new run", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({ runs: [run(6)] });
    handlers["GET /api/projects/alpha/scans/6/events"] = () => sse([endFrame(1, "ok", { elapsed_sec: 1 })]);
    handlers["POST /api/projects/alpha/scans"] = () => ({ status: 202, body: { run_id: 8 } });
    handlers["GET /api/projects/alpha/scans/8/events"] = () => sse([]);
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/scans/6");
    await screen.findByRole("button", { name: "Rescan" });
    await quickRescan(user);
    await waitFor(() => expect(here(router)).toBe("/p/alpha/scans/8"));
  });
});

// ---- screen 8: history ----------------------------------------------------------------------

describe("Scan history (screen 8)", () => {
  it("lists runs with trigger, status and duration, each linking to the run", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({
      runs: [
        run(9, { trigger: "hook", analyze: false, summary: { status: "ok", elapsed_sec: 6, analyze_skipped: null } }),
        run(8, { status: "interrupted", finished_at: null, summary: null }),
        run(7, { trigger: "initial", status: "failed", summary: { error: "boom" } }),
        run(6, { kind: "sync", trigger: "poll", summary: { moved: false, elapsed_sec: 2 } }),
      ],
    });
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/scans");
    const table = await screen.findByTestId("scan-history");
    const r9 = within(table).getByTestId("run-9");
    expect(r9).toHaveTextContent("Git hook");
    expect(r9).toHaveTextContent("Succeeded");
    expect(r9).toHaveTextContent("6.0s");
    expect(r9).toHaveTextContent("Structure only");
    expect(within(table).getByTestId("run-8")).toHaveTextContent("Interrupted");
    expect(within(table).getByTestId("run-7")).toHaveTextContent("Initial");
    expect(within(table).getByTestId("run-7")).toHaveTextContent("boom");
    expect(within(table).getByTestId("run-6")).toHaveTextContent("Already up to date");

    await user.click(within(r9).getByRole("link", { name: "#9" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/scans/9"));
  });

  it("shows an empty state, and Scan now opens the queued run", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({ runs: [] });
    handlers["POST /api/projects/alpha/scans"] = () => ({ status: 202, body: { run_id: 1 } });
    handlers["GET /api/projects/alpha/scans/1/events"] = () => sse([]);
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/scans");
    expect(await screen.findByText("No scans yet")).toBeInTheDocument();
    await quickRescan(user);
    await waitFor(() => expect(here(router)).toBe("/p/alpha/scans/1"));
  });

  it("a symlinked project answers 409 unsafe_path with the instruction", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({
      status: 409,
      body: {
        error: "refusing to use /repos/alpha/.whygraph is a symbolic link: WhyGraph never follows a symbolic link out of the repository",
        code: "unsafe_path",
        path: "/repos/alpha/.whygraph",
      },
    });
    mount("/p/alpha/scans");
    // The project layout reads the same cached error and shows the instruction
    // in place of the page.
    const err = await screen.findByTestId("problem-unsafe_path");
    expect(err).toHaveTextContent("A symbolic link is in the way");
    expect(err).toHaveTextContent("Replace the link with a real file or folder");
    expect(err).toHaveTextContent("/repos/alpha/.whygraph");
  });
});

// ---- screen 9a: overview --------------------------------------------------------------------

describe("Project overview (screen 9a)", () => {
  it("shows stats, recent scans and the per-agent connect snippet with the MCP URL", async () => {
    handlers["GET /api/projects/alpha/scans"] = () => ({ runs: [run(6), run(5, { trigger: "hook" })] });
    const user = userEvent.setup();
    mount("/p/alpha");
    const stats = await screen.findByTestId("stats");
    expect(stats).toHaveTextContent("240");
    expect(stats).toHaveTextContent("74%");
    expect(stats).toHaveTextContent("50 · 12");
    expect(stats).toHaveTextContent("31");
    const recent = await screen.findByTestId("recent-scans");
    expect(within(recent).getByText("Git hook")).toBeInTheDocument();

    expect(screen.getByTestId("mcp-url")).toHaveTextContent("http://127.0.0.1:8765/mcp/alpha");
    // Claude Code is configured, so its tab is open with the interpolated port form.
    expect(await screen.findByText(/\$\{WHYGRAPH_PORT:-8765\}/)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Codex" }));
    expect(await screen.findByText(/\[mcp_servers\.whygraph\]/)).toBeInTheDocument();
  });

  it("a stale GitHub project offers Scan now only (it fetches first; there is no Sync now)", async () => {
    handlers["GET /api/projects/alpha"] = () =>
      details("alpha", { source: "github", remote_url: "https://github.com/o/r", stale: { commits_behind: 3 } });
    handlers["POST /api/projects/alpha/scans"] = () => ({ status: 202, body: { run_id: 12 } });
    handlers["GET /api/projects/alpha/scans/12/events"] = () => sse([]);
    const user = userEvent.setup();
    const { router } = mount("/p/alpha");
    const banner = await screen.findByTestId("stale-banner");
    expect(banner).toHaveTextContent("3 commits behind");
    expect(screen.queryByRole("button", { name: "Sync now" })).toBeNull();
    await user.click(within(banner).getByRole("button", { name: "Rescan" }));
    await user.click(await screen.findByRole("menuitem", { name: "Quick rescan" }));
    await waitFor(() => expect(calls("POST", "/api/projects/alpha/scans")).toHaveLength(1));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/scans/12"));
    expect(calls("POST", "/api/projects/alpha/sync")).toHaveLength(0);
  });

  it("shows the describe-cost card when more than 500 commits wait", async () => {
    handlers["GET /api/projects/alpha/scan-estimate"] = () => ({
      commits: 900,
      upper_bound: true,
      large_commits: 0,
      model: { provider: "anthropic", model: "claude-haiku-4-5" },
      tokens: { input: 1000, output: 100, input_range: { low: 1, high: 2 }, output_range: { low: 1, high: 2 } },
      cost: null,
      missing_key: null,
    });
    handlers["POST /api/projects/alpha/scans"] = () => ({ status: 202, body: { run_id: 3 } });
    handlers["GET /api/projects/alpha/scans/3/events"] = () => sse([]);
    const user = userEvent.setup();
    mount("/p/alpha");
    expect(await screen.findByTestId("scan-estimate")).toHaveTextContent("900 commits to describe");
    await user.click(screen.getByRole("button", { name: "Describe now" }));
    await waitFor(() => expect(calls("POST", "/api/projects/alpha/scans")[0]?.body).toEqual({ trigger: "describe" }));
  });
});

// ---- screen 12: edge states -----------------------------------------------------------------

describe("Edge states (screen 12)", () => {
  it("a missing folder shows the fix command when the folder is no longer shared", async () => {
    handlers["GET /api/projects/alpha"] = () => details("alpha", { root_status: "missing", stats: null, detected: null });
    handlers["POST /api/portal/check-path"] = () => ({
      path: "/repos/alpha",
      shared: false,
      is_git: false,
      protected: false,
      folder_suggestion: "/repos",
      command: "whygraph up --add-folder /repos",
      github: null,
    });
    mount("/p/alpha/explorer");
    const fix = await screen.findByTestId("unshared-fix");
    expect(fix).toHaveTextContent("whygraph up --add-folder /repos");
    expect(screen.getByTestId("project-unavailable")).toHaveTextContent("/repos/alpha");
    // No data request is fired for the unusable project.
    expect(log.some((c) => c.path.includes("/graph/") || c.path.endsWith("/tree"))).toBe(false);
  });

  it("a shared-but-empty folder says the repository moved", async () => {
    handlers["GET /api/projects/alpha"] = () => details("alpha", { root_status: "missing", stats: null, detected: null });
    handlers["POST /api/portal/check-path"] = () => ({
      path: "/repos/alpha",
      shared: true,
      is_git: false,
      protected: false,
      folder_suggestion: null,
      command: null,
      github: null,
    });
    mount("/p/alpha/scans");
    await waitFor(() =>
      expect(screen.getByTestId("project-unavailable")).toHaveTextContent("moved or deleted"),
    );
  });

  it("an uninitialized project points at setup", async () => {
    handlers["GET /api/projects/alpha"] = () =>
      details("alpha", { initialized: false, initialized_at: null, stats: null, last_scan_at: null });
    mount("/p/alpha/chat");
    const notice = await screen.findByTestId("not-initialized");
    expect(within(notice).getByRole("link", { name: "Finish setup" })).toHaveAttribute("href", "/p/alpha/init");
  });

  it("a symlink in the way is explained on Explorer, not shown as a bare error", async () => {
    handlers["GET /api/projects/alpha"] = () => details("alpha", { stats: null });
    handlers["GET /api/projects/alpha/scans"] = () => ({
      status: 409,
      body: {
        error: "refusing to use /repos/alpha/.codegraph is a symbolic link: WhyGraph never follows a symbolic link out of the repository",
        code: "unsafe_path",
        path: "/repos/alpha/.codegraph",
      },
    });
    mount("/p/alpha/explorer");
    const problem = await screen.findByTestId("problem-unsafe_path");
    expect(problem).toHaveTextContent("/repos/alpha/.codegraph");
    expect(problem).toHaveTextContent("Replace the link");
  });

  it("the degraded portal page explains the failure", async () => {
    handlers["GET /api/portal/state"] = () => ({ error: "alembic: no such table" });
    mount("/");
    const page = await screen.findByTestId("degraded-page");
    expect(page).toHaveTextContent("alembic: no such table");
    expect(page).toHaveTextContent("whygraph logs");
  });
});

// ---- screen 10: project settings --------------------------------------------------------------

describe("Project settings (screen 10)", () => {
  beforeEach(() => {
    handlers["GET /api/projects/alpha/config"] = () => ({
      config: {},
      secrets: emptySecrets(),
      import: { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] },
    });
    handlers["GET /api/portal/defaults"] = () => ({ config: {}, secrets: emptySecrets(), no_provider_key: false });
    handlers["POST /api/projects/alpha/init"] = (body) => ({
      dry_run: body?.dry_run === true,
      gitignore_added: [],
      hooks: null,
      hooks_error: null,
      agent_files: [
        { file: ".mcp.json", status: "skip", agent: "claude", reason: null, snippet: null, diff: null },
      ],
      asset_files: [],
      configured_agents: (body?.agents as string[]) ?? [],
      needs_confirmation: [],
      refused: [],
      marker_written: body?.dry_run !== true,
      initialized: body?.dry_run !== true,
      custom_db_paths: [],
    });
  });

  it("renames the project", async () => {
    handlers["PATCH /api/projects/alpha"] = (body) => details("alpha", { name: body?.name });
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    const input = await screen.findByLabelText("Display name");
    await user.clear(input);
    await user.type(input, "Alpha team");
    await user.click(screen.getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(calls("PATCH", "/api/projects/alpha")).toHaveLength(1));
    expect(calls("PATCH", "/api/projects/alpha")[0].body).toEqual({ name: "Alpha team" });
  });

  it("shows the config form (project scope) and the configured agents", async () => {
    mount("/p/alpha/settings");
    expect(await screen.findByTestId("config-form")).toBeInTheDocument();
    const agents = await screen.findByRole("group", { name: "Agents" });
    expect(within(agents).getByRole("checkbox", { name: /Claude Code/ })).toBeChecked();
    expect(within(agents).getByRole("checkbox", { name: /Cursor/ })).not.toBeChecked();
  });

  it("Update agent files previews with force, warns about local edits, then applies with force", async () => {
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await user.click(await screen.findByRole("checkbox", { name: /Update agent files/ }));
    expect(await screen.findByTestId("force-warning")).toHaveTextContent("Local edits to these files are replaced");
    // The preview is a dry run carrying force; nothing is written yet.
    await waitFor(() =>
      expect(
        calls("POST", "/api/projects/alpha/init").some((c) => c.body?.dry_run === true && c.body?.force === true),
      ).toBe(true),
    );
    expect(calls("POST", "/api/projects/alpha/init").some((c) => !c.body?.dry_run)).toBe(false);

    await user.click(screen.getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(calls("POST", "/api/projects/alpha/init").some((c) => !c.body?.dry_run)).toBe(true));
    const applied = calls("POST", "/api/projects/alpha/init").find((c) => !c.body?.dry_run)!;
    expect(applied.body).toMatchObject({ agents: ["claude"], force: true });
    expect(await screen.findByTestId("init-done")).toHaveTextContent("Agent files updated");
  });

  it("unticking a configured agent removes its entry; ticking another adds it", async () => {
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    const agents = await screen.findByRole("group", { name: "Agents" });
    await user.click(within(agents).getByRole("checkbox", { name: /Claude Code/ }));
    await user.click(within(agents).getByRole("checkbox", { name: /Cursor/ }));
    expect(await screen.findByTestId("removal-note")).toHaveTextContent("claude");
    await waitFor(() =>
      expect(
        calls("POST", "/api/projects/alpha/init").some(
          (c) =>
            c.body?.dry_run === true &&
            JSON.stringify(c.body?.agents) === '["cursor"]' &&
            JSON.stringify(c.body?.agent_actions) === '{"claude":"remove"}',
        ),
      ).toBe(true),
    );
    await user.click(screen.getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(calls("POST", "/api/projects/alpha/init").some((c) => !c.body?.dry_run)).toBe(true));
    const applied = calls("POST", "/api/projects/alpha/init").find((c) => !c.body?.dry_run)!;
    expect(applied.body).toMatchObject({ agents: ["cursor"], agent_actions: { claude: "remove" } });
  });

  it("removes a local project, with the agent-entry checkbox, and says what stays", async () => {
    handlers["DELETE /api/projects/alpha"] = () => ({
      removed: "alpha",
      hooks: null,
      agent_files: [],
      checkout_deleted: false,
      warnings: ["left .mcp.json: symbolic link"],
    });
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/settings");
    await user.click(await screen.findByRole("button", { name: "Remove project" }));
    const dialog = await screen.findByTestId("remove-dialog");
    expect(dialog).toHaveTextContent("Your repository, including its");
    expect(dialog).toHaveTextContent(".whygraph/");
    await user.click(within(dialog).getByRole("checkbox", { name: /Also remove the agent MCP entries/ }));
    await user.click(within(dialog).getByRole("button", { name: "Remove project" }));
    await waitFor(() => expect(calls("DELETE", "/api/projects/alpha")).toHaveLength(1));
    expect(calls("DELETE", "/api/projects/alpha")[0].body).toEqual({
      strip_agent_entries: true,
      confirm_tracked: [],
    });
    const done = await screen.findByTestId("remove-done");
    expect(done).toHaveTextContent("symbolic link");
    await user.click(within(done).getByRole("button", { name: "Back to projects" }));
    await waitFor(() => expect(here(router)).toBe("/"));
  });

  it("a GitHub project needs its name typed, and a tracked file is confirmed from the 409", async () => {
    handlers["GET /api/projects/alpha"] = () =>
      details("alpha", { source: "github", remote_url: "https://github.com/o/r", name: "Alpha" });
    let attempt = 0;
    handlers["DELETE /api/projects/alpha"] = (body) => {
      attempt += 1;
      if (attempt === 1) {
        return {
          status: 409,
          body: {
            error: "confirm the git-tracked agent files first",
            code: "needs_confirmation",
            agent_files: [
              { file: ".mcp.json", status: "needs_confirmation", agent: "claude", reason: null, snippet: null, diff: "-a\n+b" },
            ],
          },
        };
      }
      return { removed: "alpha", hooks: null, agent_files: [], checkout_deleted: true, warnings: [], echo: body };
    };
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await user.click(await screen.findByRole("button", { name: "Remove project" }));
    const dialog = await screen.findByTestId("remove-dialog");
    const remove = within(dialog).getByRole("button", { name: "Remove project" });
    expect(remove).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/to confirm deleting the checkout/), "Alpha");
    await user.click(within(dialog).getByRole("checkbox", { name: /Also remove the agent MCP entries/ }));
    expect(remove).toBeEnabled();
    await user.click(remove);

    const confirm = await screen.findByTestId("remove-confirm-tracked");
    expect(confirm).toHaveTextContent(".mcp.json");
    expect(within(dialog).getByRole("button", { name: "Remove project" })).toBeDisabled();
    await user.click(within(confirm).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Remove project" }));
    await waitFor(() => expect(calls("DELETE", "/api/projects/alpha")).toHaveLength(2));
    expect(calls("DELETE", "/api/projects/alpha")[1].body).toEqual({
      strip_agent_entries: true,
      confirm_tracked: [".mcp.json"],
      confirm_name: "Alpha",
    });
    expect(await screen.findByTestId("remove-done")).toHaveTextContent("The checkout was deleted");
  });

  it("removal is blocked while a scan runs", async () => {
    handlers["GET /api/projects/alpha"] = () =>
      details("alpha", { running_scan: { id: 6, status: "running", trigger: "manual" } });
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await user.click(await screen.findByRole("button", { name: "Remove project" }));
    const dialog = await screen.findByTestId("remove-dialog");
    expect(dialog).toHaveTextContent("A scan is queued or running");
    expect(within(dialog).getByRole("button", { name: "Remove project" })).toBeDisabled();
  });
});

// ---- screen 11: global settings ----------------------------------------------------------------

describe("Global settings (screen 11)", () => {
  it("shows the no-provider-key banner and lists project keys cleared by an endpoint change", async () => {
    handlers["GET /api/portal/defaults"] = () => ({ config: {}, secrets: emptySecrets(), no_provider_key: true });
    handlers["PUT /api/portal/defaults"] = () => ({
      config: { llm: { openai: { base_url: "http://host.docker.internal:1234/v1" } } },
      secrets: emptySecrets(),
      no_provider_key: true,
      cleared_project_keys: [{ slug: "alpha", provider: "openai" }],
    });
    const user = userEvent.setup();
    mount("/settings");
    expect(await screen.findByTestId("no-provider-key")).toHaveTextContent("Keys in your shell");
    await user.type(
      await screen.findByLabelText("OpenAI-compatible base URL"),
      "http://host.docker.internal:1234/v1",
    );
    await user.click(screen.getByRole("button", { name: "Save" }));
    const cleared = await screen.findByTestId("cleared-keys");
    expect(cleared).toHaveTextContent("alpha");
    expect(cleared).toHaveTextContent("openai");
    expect(within(cleared).getByRole("link", { name: "alpha" })).toHaveAttribute("href", "/p/alpha/settings");
    expect(calls("PUT", "/api/portal/defaults")).toHaveLength(1);
  });
});
