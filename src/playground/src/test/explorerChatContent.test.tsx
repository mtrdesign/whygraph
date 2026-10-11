import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { createAppRouter } from "../router";
import { useUi } from "../store";
import { ThemeProvider } from "../theme";
import { ReactFlowProvider } from "@xyflow/react";
import { OverviewNode } from "../components/OverviewNode";

// M2f-3 S15 (section 6.4 "Explorer / Chat content"): the phone Explorer's tabs, the deep link
// without `file`, the Rationale tab's state matrix, the chat no-key notice and hard-stop selects,
// the token footer, the session cost and the worded SSE error frame. Through the real router.

// The canvases need a layout engine jsdom lacks; the panes under test are the tabs around them.
vi.mock("../components/GraphCanvas", () => ({ GraphCanvas: () => <div data-testid="graph-canvas" /> }));
vi.mock("../components/Overview", () => ({ Overview: () => <div data-testid="graph-overview" /> }));

interface Fake {
  mode: "local" | "production";
  project: Record<string, unknown>;
  providers: { provider: string; configured: boolean; default_model: string; env_var: string | null }[];
  rationale: Record<string, unknown>;
  calls: string[];
  usage: Response | null;
  /** Session costs the usage API answers in turn (the last one repeats). */
  costs?: number[];
  /** The provider list answers this status instead (ER-4). */
  providersStatus?: number;
  stream: string[];
  transcript: unknown[];
  role: string;
}
let fake: Fake;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const enc = new TextEncoder();

const SYMBOL = {
  id: "n1",
  qualified_name: "src/app/mod.py::run",
  name: "run",
  kind: "function",
  file_path: "src/app/mod.py",
  start_line: 3,
  end_line: 9,
  signature: null,
};

function baseProject(over: Record<string, unknown> = {}) {
  return {
    slug: "alpha",
    name: "Alpha",
    source: "local",
    root: "/repos/alpha",
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
    mcp_url: null,
    stats: { commits: 3 },
    ...over,
  };
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const path = url.pathname;
  const method = init?.method ?? "GET";
  fake.calls.push(`${method} ${path}${url.search}`);
  if (path === "/api/portal/state") {
    return Promise.resolve(
      json(
        fake.mode === "production"
          ? {
              mode: "production",
              setup_complete: true,
              host_kind: "org",
              base_url: "https://whygraph.test",
              user: { uid: "u1", display_name: "T", role: "user" },
              org: { slug: "acme", name: "Acme", role: fake.role },
              port: 8765,
              shared_folders: [],
            }
          : {
              mode: "local",
              setup_complete: true,
              user: { uid: "u1", display_name: "T", role: "owner" },
              port: 8765,
              shared_folders: [],
            },
      ),
    );
  }
  if (path === "/api/projects") return Promise.resolve(json({ projects: [fake.project] }));
  if (path === "/api/projects/alpha") return Promise.resolve(json(fake.project));
  if (path === "/api/usage" || path === "/api/usage/me") {
    const cost = fake.costs ? (fake.costs.length > 1 ? fake.costs.shift()! : fake.costs[0]) : 0.84;
    return Promise.resolve(
      fake.usage ??
        json({
          totals: { calls: 2, cost_usd: cost },
          split: {},
          series: [],
          groups: [],
        }),
    );
  }
  const m = /^\/api\/projects\/alpha(\/.*)$/.exec(path);
  if (!m) return Promise.resolve(json({ error: "unhandled", code: "x" }, 500));
  const rest = m[1];
  if (rest === "/tree") {
    const dir = url.searchParams.get("dir");
    if (dir === "src")
      return Promise.resolve(
        json({ entries: [{ id: "dir:src/app", label: "app", kind: "directory", has_children: true, dir: "src/app" }] }),
      );
    if (dir === "src/app")
      return Promise.resolve(
        json({
          entries: [
            {
              id: "file:mod",
              label: "mod.py",
              kind: "file",
              has_children: false,
              qualified_name: SYMBOL.qualified_name,
              path: SYMBOL.file_path,
            },
          ],
        }),
      );
    return Promise.resolve(
      json({ entries: [{ id: "dir:src", label: "src", kind: "directory", has_children: true, dir: "src" }] }),
    );
  }
  if (rest === "/node") {
    return Promise.resolve(json({ symbol: SYMBOL, analyzed: false, relations: { callers: [], callees: [], imports: [], container: null, children: [] } }));
  }
  if (rest === "/node/rationale") return Promise.resolve(json(fake.rationale));
  if (rest === "/node/evidence") return Promise.resolve(json({ evidence: [] }));
  if (rest === "/history") return Promise.resolve(json({ evidence: [] }));
  if (rest === "/chat/providers") {
    if (fake.providersStatus) return Promise.resolve(json({ error: "boom", code: "x" }, fake.providersStatus));
    return Promise.resolve(json(fake.providers));
  }
  if (rest === "/chat/models") {
    return Promise.resolve(json({ provider: "openai", source: "live", default_model: "gpt-x", models: [{ id: "gpt-x", display_name: "GPT X" }] }));
  }
  if (rest === "/chat/sessions" && method === "GET") {
    return Promise.resolve(
      json([
        {
          id: 4,
          title: "Costly chat",
          provider: "openai",
          model: "gpt-x",
          created_at: "2026-10-01T10:00:00Z",
          updated_at: "2026-10-01T10:05:00Z",
          message_count: 2,
        },
      ]),
    );
  }
  if (rest === "/chat/sessions/4" && method === "GET") {
    return Promise.resolve(json({ id: 4, title: "Costly chat", provider: "openai", model: "gpt-x", created_at: "", updated_at: "", messages: fake.transcript }));
  }
  if (rest === "/chat/sessions/4/messages") {
    // The server persists a failed turn with the provider's raw text on its row.
    if (fake.stream.some((f) => f.includes('"error"')))
      fake.transcript = [
        { id: 1, role: "user", content: "hello", tool_calls: [], tool_call_id: null, error: null, model: null, input_tokens: null, output_tokens: null },
        { id: 2, role: "assistant", content: "", tool_calls: [], tool_call_id: null, error: "429 rate limit hit upstream", model: "gpt-x", input_tokens: null, output_tokens: null },
      ];
    return Promise.resolve(
      new Response(
        new ReadableStream<Uint8Array>({
          start(c) {
            for (const f of fake.stream) c.enqueue(enc.encode(`data: ${f}\n\n`));
            c.close();
          },
        }),
        { status: 200, headers: { "content-type": "text/event-stream" } },
      ),
    );
  }
  return Promise.resolve(json({ error: "unhandled", code: "x" }, 500));
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

function setPhone(phone: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((q: string) => ({
      matches: phone && q.includes("max-width"),
      addEventListener() {},
      removeEventListener() {},
    })),
  );
}

beforeEach(() => {
  fake = {
    mode: "local",
    project: baseProject(),
    providers: [{ provider: "openai", configured: true, default_model: "gpt-x", env_var: null }],
    rationale: { status: "not_generated" },
    calls: [],
    usage: null,
    stream: [],
    transcript: [],
    role: "owner",
  };
  window.localStorage.clear();
  useUi.setState({ paletteOpen: false, navOpen: false, streamingSessionId: null });
  setPhone(false);
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the phone Explorer", () => {
  it("has an sr-only h1 and the Tree / Graph / Details tabs, and a selection opens Details", async () => {
    setPhone(true);
    const router = mount("/p/alpha/explorer");
    expect(await screen.findByRole("heading", { level: 1, name: "Explorer - Alpha" })).toBeInTheDocument();
    const tabs = screen.getByRole("tablist", { name: "Explorer panes" });
    expect(within(tabs).getAllByRole("tab").map((t) => t.textContent)).toEqual(["Tree", "Graph", "Details"]);
    expect(within(tabs).getByRole("tab", { name: "Tree" })).toHaveAttribute("aria-selected", "true");

    await userEvent.click(await screen.findByText("src"));
    await userEvent.click(await screen.findByText("app"));
    await userEvent.click(await screen.findByText("mod.py"));

    await waitFor(() => expect(within(tabs).getByRole("tab", { name: "Details" })).toHaveAttribute("aria-selected", "true"));
    expect(router.state.location.search).toMatchObject({ node: SYMBOL.qualified_name, file: SYMBOL.file_path });
    expect(await screen.findByRole("tab", { name: "Relationships" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Back to graph" }));
    expect(await screen.findByTestId("graph-canvas")).toBeInTheDocument();
    expect(within(tabs).getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
    // The selection stays in the URL.
    expect(router.state.location.search).toMatchObject({ node: SYMBOL.qualified_name });
  });

  it("opens straight on Details for a shared link", async () => {
    setPhone(true);
    mount(`/p/alpha/explorer?node=${encodeURIComponent(SYMBOL.qualified_name)}&file=${encodeURIComponent(SYMBOL.file_path)}`);
    expect(await screen.findByRole("button", { name: "Back to graph" })).toBeInTheDocument();
  });

  it("keeps the desktop panes on a wide screen (no pane tabs)", async () => {
    mount("/p/alpha/explorer");
    await screen.findByRole("heading", { level: 1, name: "Explorer - Alpha" });
    expect(screen.queryByRole("tablist", { name: "Explorer panes" })).toBeNull();
  });
});

describe("BUG-17: ?node= without file", () => {
  it("resolves the file from the node and expands the tree to it", async () => {
    mount(`/p/alpha/explorer?node=${encodeURIComponent(SYMBOL.qualified_name)}`);
    // The tree opened src, then src/app, down to the file's row.
    expect(await screen.findByText("mod.py")).toBeInTheDocument();
    expect(fake.calls.some((c) => c.includes("/tree?dir=src%2Fapp"))).toBe(true);
  });

  it("the selected tree row keeps its tint under the pointer (no grey hover on it)", async () => {
    mount(`/p/alpha/explorer?node=${encodeURIComponent(SYMBOL.qualified_name)}`);
    const row = (await screen.findByText("mod.py")).parentElement!;
    await waitFor(() => expect(row.className).toContain("bg-primary-soft"));
    expect(row.className).not.toContain("hover:bg-accent");
    expect(screen.getByText("src").parentElement!.className).toContain("hover:bg-accent");
  });
});

describe("the Explorer graph", () => {
  it("says '0 of 2 explained' in words on a canvas node, not a bare '0/2' (EXC-6)", () => {
    render(
      <ReactFlowProvider>
        <OverviewNode
          {...({
            id: "d",
            data: { label: "src", kind: "directory", coverage: { analyzed: 0, total: 2, fraction: 0 }, internal_edges: 0 },
          } as unknown as Parameters<typeof OverviewNode>[0])}
        />
      </ReactFlowProvider>,
    );
    expect(screen.getByTestId("overview-node-coverage")).toHaveTextContent("0 of 2 explained");
    expect(screen.queryByText("0/2")).toBeNull();
  });
});

describe("the Rationale tab states", () => {
  const open = async () => {
    mount(`/p/alpha/explorer?node=${encodeURIComponent(SYMBOL.qualified_name)}&file=${encodeURIComponent(SYMBOL.file_path)}`);
    await userEvent.click(await screen.findByRole("tab", { name: "Rationale" }));
  };

  it("offers Generate when it can", async () => {
    await open();
    expect(await screen.findByRole("button", { name: "Generate rationale" })).toBeEnabled();
  });

  it("says there is no history to explain, with no dead button, and names the Overview", async () => {
    fake.rationale = { status: "no_evidence" };
    await open();
    expect(await screen.findByText(/No history to explain yet: this symbol has no commits in the scanned history\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate rationale" })).toBeNull();
    const hint = screen.getByTestId("rationale-scan-hint");
    expect(hint).toHaveTextContent("project Overview");
    expect(within(hint).getByText("whygraph scan").tagName).toBe("CODE");
    expect(hint.textContent).not.toContain("`");
  });

  it("leaves whygraph scan out of the hint in production", async () => {
    fake.mode = "production";
    fake.rationale = { status: "no_evidence" };
    await open();
    const hint = await screen.findByTestId("rationale-scan-hint");
    expect(hint).not.toHaveTextContent("whygraph scan");
  });

  it("counts a card's evidence with plurals: 1 commit, not 1 commits", async () => {
    fake.rationale = {
      status: "cached",
      purpose: "p",
      why: "w",
      provider: "anthropic",
      evidence_count: { commits: 1, prs: 2, issues: 1 },
    };
    await open();
    expect(await screen.findByText(/1 commit, 2 PRs, 1 issue$/)).toBeInTheDocument();
  });

  it("dates a cached card in words, never a raw ISO stamp", async () => {
    fake.rationale = { status: "cached", purpose: "p", why: "w", provider: "anthropic", cached_at: "2026-10-03T14:05:00Z" };
    await open();
    const line = await screen.findByText(/ · generated /);
    expect(line.textContent).not.toContain("2026-10-03T14:05");
    expect(line.textContent).toMatch(/generated \S/);
  });

  it("asks for a key (admin: with a Settings link; others: ask an admin)", async () => {
    fake.project = baseProject({ missing_key: "anthropic" });
    await open();
    const note = await screen.findByTestId("rationale-no-key");
    expect(note).toHaveTextContent("Add an Anthropic key to generate rationale");
    expect(within(note).getByRole("link", { name: "Open Settings" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate rationale" })).toBeNull();
  });

  it("tells a project member without configure rights to ask an admin", async () => {
    fake.project = baseProject({
      missing_key: "anthropic",
      permissions: PROJECT_ACTIONS.filter((a) => a !== "project.configure"),
    });
    await open();
    expect(await screen.findByTestId("rationale-no-key")).toHaveTextContent("Ask a project admin to add a key.");
  });

  it("says viewers can read but not generate", async () => {
    fake.project = baseProject({ permissions: ["project.read"] });
    await open();
    expect(await screen.findByTestId("rationale-viewer")).toHaveTextContent("Viewers can read cards but not generate them.");
  });

  it("shows the budget line with a disabled button under a hard stop", async () => {
    fake.project = baseProject({ llm_block: "budget_exceeded", llm_block_scope: "project" });
    await open();
    expect(await screen.findByTestId("generate-blocked")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate rationale" })).toBeDisabled();
  });
});

describe("Evidence and History empty states", () => {
  it("name the next step", async () => {
    mount(`/p/alpha/explorer?node=${encodeURIComponent(SYMBOL.qualified_name)}&file=${encodeURIComponent(SYMBOL.file_path)}`);
    await userEvent.click(await screen.findByRole("tab", { name: "Evidence" }));
    expect(await screen.findByText(/No commits touch this symbol in the scanned history\. Rescan after new commits\./)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "History" }));
    expect(await screen.findByText(/Rescan after new commits\./)).toBeInTheDocument();
  });
});

describe("Chat content", () => {
  it("shows the no-key notice, a disabled composer and the reason", async () => {
    fake.providers = [{ provider: "openai", configured: false, default_model: "gpt-x", env_var: "OPENAI_API_KEY" }];
    mount("/p/alpha/chat");
    const notice = await screen.findByTestId("chat-no-key");
    expect(notice).toHaveTextContent("No OpenAI key. Add one in Settings > Models and keys.");
    expect(within(notice).getByRole("link", { name: /Settings/ })).toHaveAttribute("href", "/settings");
    const box = screen.getByRole("textbox");
    expect(box).toBeDisabled();
    expect(box).toHaveAttribute("aria-describedby", "composer-notice");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    // The provider select names the provider and that it has no key, never an env var.
    const provider = screen.getByRole("combobox", { name: "Provider" });
    await waitFor(() => expect(provider).toHaveTextContent("OpenAI (no key)"));
    expect(document.body).not.toHaveTextContent("OPENAI_API_KEY");
  });

  it("asks a non-owner in production to ask an owner", async () => {
    fake.mode = "production";
    fake.role = "member";
    fake.providers = [{ provider: "openai", configured: false, default_model: "gpt-x", env_var: null }];
    mount("/p/alpha/chat");
    expect(await screen.findByTestId("chat-no-key")).toHaveTextContent("No OpenAI key. Ask an owner to add one.");
  });

  it("leaves the composer alone when the provider has a key", async () => {
    mount("/p/alpha/chat");
    await screen.findByRole("combobox", { name: "Model" });
    expect(screen.queryByTestId("chat-no-key")).toBeNull();
    expect(screen.getByRole("textbox")).toBeEnabled();
  });

  it("disables the model selects under a hard stop", async () => {
    fake.project = baseProject({ llm_block: "budget_exceeded", llm_block_scope: "project" });
    mount("/p/alpha/chat/4");
    const model = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(model).toBeDisabled());
    expect(screen.getByRole("combobox", { name: "Provider" })).toBeDisabled();
  });

  it("separates the token footer", async () => {
    fake.transcript = [
      { id: 1, role: "user", content: "hi", tool_calls: [], tool_call_id: null, error: null, model: null, input_tokens: null, output_tokens: null },
      { id: 2, role: "assistant", content: "hello", tool_calls: [], tool_call_id: null, error: null, model: "gpt-x", input_tokens: 31500, output_tokens: 2250 },
    ];
    mount("/p/alpha/chat/4");
    expect(await screen.findByTestId("turn-tokens")).toHaveTextContent("31,500 in · 2,250 out");
  });

  it("shows this chat's cost beside the title, from the org usage locally", async () => {
    mount("/p/alpha/chat/4");
    expect(await screen.findByTestId("chat-cost")).toHaveTextContent("This chat: ~$0.84");
    const call = fake.calls.find((c) => c.startsWith("GET /api/usage"));
    expect(call).toContain("/api/usage?");
    expect(call).toContain("project=alpha");
    expect(call).toContain("chat_session=4");
    expect(call).toContain("from=2026-10-01");
  });

  it("asks again for a zero cost after a turn, and shows it once the ledger has it (EXC-4)", async () => {
    // The first answers come before the usage rows are written (a zero), then the priced total.
    fake.costs = [0, 0.05];
    mount("/p/alpha/chat/4");
    await screen.findByTestId("chat-title");
    expect(screen.queryByTestId("chat-cost")).toBeNull();
    expect(await screen.findByTestId("chat-cost", {}, { timeout: 5000 })).toHaveTextContent("This chat: ~$0.05");
    expect(fake.calls.filter((c) => c.startsWith("GET /api/usage")).length).toBeGreaterThanOrEqual(2);
  });

  it("disables the composer and the starter prompts with a reason when the providers fail (ER-4)", async () => {
    fake.providersStatus = 500;
    mount("/p/alpha/chat");
    expect(await screen.findByText("Couldn't load the chat providers")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeDisabled();
    // R3: the placeholder gives the real reason, not "Add a key".
    expect(screen.getByRole("textbox")).toHaveAttribute("placeholder", "Chat providers couldn't load");
    expect(screen.getByTestId("chat-no-key")).toHaveTextContent("Chat can't send until the provider list loads");
    for (const text of ["What changed most in the last month?", "Explain how this project is structured"]) {
      expect(screen.getByRole("button", { name: text })).toBeDisabled();
    }
    // Retry brings the list (and the composer) back.
    fake.providersStatus = undefined;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("textbox")).toBeEnabled());
    expect(screen.getByRole("button", { name: "What changed most in the last month?" })).toBeEnabled();
  });

  it("disables the starter prompts when the provider has no key", async () => {
    fake.providers = [{ provider: "openai", configured: false, default_model: "gpt-x", env_var: null }];
    mount("/p/alpha/chat");
    await screen.findByTestId("chat-no-key");
    expect(screen.getByRole("button", { name: "Which areas have no rationale yet?" })).toBeDisabled();
  });

  it("reads /api/usage/me in production and hides the cost on a 403", async () => {
    fake.mode = "production";
    fake.usage = json({ error: "no", code: "forbidden" }, 403);
    mount("/p/alpha/chat/4");
    await screen.findByTestId("chat-title");
    await waitFor(() => expect(fake.calls.some((c) => c.startsWith("GET /api/usage/me?"))).toBe(true));
    expect(screen.queryByTestId("chat-cost")).toBeNull();
  });

  it("words an SSE error frame through the registry and keeps the provider text under Show details", async () => {
    fake.stream = [
      JSON.stringify({ type: "error", message: "429 rate limit hit upstream", code: "provider_error" }),
      JSON.stringify({ type: "done", input_tokens: 1, output_tokens: 1 }),
    ];
    mount("/p/alpha/chat/4");
    await userEvent.type(await screen.findByRole("textbox"), "hello{Enter}");
    expect(await screen.findByText(/The model provider returned an error/)).toBeInTheDocument();
    expect(screen.getByTestId("error-details")).not.toHaveAttribute("open");
    const details = screen.getByTestId("error-details");
    expect(within(details).getByText("Show details")).toBeInTheDocument();
    expect(details).toHaveTextContent("429 rate limit hit upstream");
  });

  it("wraps tool-card JSON", async () => {
    fake.transcript = [
      { id: 1, role: "user", content: "hi", tool_calls: [], tool_call_id: null, error: null, model: null, input_tokens: null, output_tokens: null },
      { id: 2, role: "assistant", content: "", tool_calls: [{ id: "c1", name: "find", arguments: { q: "x" } }], tool_call_id: null, error: null, model: "gpt-x", input_tokens: 1, output_tokens: 1 },
      { id: 3, role: "tool", content: '{"a":1}', tool_calls: [], tool_call_id: "c1", error: null, model: null, input_tokens: null, output_tokens: null },
    ];
    mount("/p/alpha/chat/4");
    await userEvent.click(await screen.findByRole("button", { name: /find/ }));
    const pre = (await screen.findByText(/"a": 1/)).closest("pre")!;
    expect(pre.className).toContain("whitespace-pre-wrap");
    expect(pre.className).toContain("break-words");
  });
});
