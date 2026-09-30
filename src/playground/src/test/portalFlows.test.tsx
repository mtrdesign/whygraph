import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

// The wizard and its neighbours end to end against a fake portal: the router
// gates, the forms, and the exact request bodies the backend would receive.

// ---- fake backend ------------------------------------------------------------------

type Json = Record<string, unknown>;

interface Fake {
  setupComplete: boolean;
  /** `shared` for check-path; flipped by a test to simulate `whygraph up --add-folder`. */
  shared: boolean;
  github: { slug: string; remote_url: string } | null;
  projects: Json[];
  config: Record<string, { config: Json; secrets: Json }>;
  defaults: Json;
  addError: { status: number; body: Json } | null;
  init: { needsConfirmation: boolean };
  events: string[];
  estimate: Json;
  /** `port_change` of `GET /api/portal/state`. */
  portChange: Json | null;
  /** `import` of `GET .../config` (the whygraph.toml import report). */
  importReport: Json | null;
  log: { method: string; path: string; body: Json | null }[];
}

let fake: Fake;

const noKey = { set: false, hint: null };
const emptySecrets = () => ({
  llm: { anthropic: noKey, openai: noKey, deepseek: noKey, openrouter: noKey, "claude-cli": noKey },
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
    stale: null,
    ...over,
  };
}

function details(slug: string, over: Json = {}): Json {
  return {
    ...summary(slug, over),
    agents: [],
    missing_key: null,
    mcp_url: `http://127.0.0.1:8765/mcp/${slug}`,
    stats: null,
    ...over,
  };
}

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

const find = (slug: string) => fake.projects.find((p) => p.slug === slug);

function dryRunFiles(needsConfirmation: boolean) {
  return [
    {
      file: ".mcp.json",
      status: needsConfirmation ? "needs_confirmation" : "write",
      agent: "claude",
      reason: null,
      snippet: null,
      diff: needsConfirmation ? '-  "command": "whygraph-mcp"\n+  "type": "http"' : null,
    },
    { file: ".vscode/mcp.json", status: "overwrite", agent: "vscode", reason: null, snippet: null, diff: "-a\n+b" },
  ];
}

function initResult(body: Json) {
  const files = dryRunFiles(fake.init.needsConfirmation);
  const confirmed = (body.confirm_tracked as string[] | undefined) ?? [];
  const pending = files.filter((f) => f.status === "needs_confirmation" && !confirmed.includes(f.file));
  const dry = body.dry_run === true;
  return {
    dry_run: dry,
    gitignore_added: dry ? [] : [".whygraph/"],
    hooks: dry ? null : { installed: ["post-commit"], removed: [], actions: {} },
    hooks_error: null,
    agent_files: files,
    asset_files: [],
    configured_agents: dry ? [] : (body.agents as string[]),
    needs_confirmation: pending.map((f) => f.file),
    refused: [],
    marker_written: !dry && pending.length === 0,
    initialized: !dry && pending.length === 0,
    custom_db_paths: [],
  };
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const path = url.pathname;
  const method = init?.method ?? "GET";
  const body = init?.body ? (JSON.parse(String(init.body)) as Json) : null;
  fake.log.push({ method, path: path + url.search, body });
  const reply = (b: unknown, status = 200) => Promise.resolve(json(b, status));

  if (path === "/api/portal/state") {
    return reply({
      mode: "local",
      setup_complete: fake.setupComplete,
      user: fake.setupComplete ? { uid: "u1", display_name: "Ada", role: "owner" } : null,
      port: (fake.portChange?.port as number | undefined) ?? 8765,
      shared_folders: ["/repos"],
      port_change: fake.portChange,
    });
  }
  if (path === "/api/portal/setup" && method === "POST") {
    fake.setupComplete = true;
    return reply({ setup_complete: true, user: { uid: "u1", display_name: body?.display_name, role: "owner" } }, 201);
  }
  if (path === "/api/portal/repos") {
    return reply({
      repos: [
        { path: "/repos/alpha", name: "alpha", registered: true },
        { path: "/repos/web", name: "web", registered: false },
      ],
      truncated: false,
    });
  }
  if (path === "/api/portal/check-path") {
    const p = String(body?.path);
    return reply({
      path: p,
      shared: fake.shared || p === "/repos/web",
      is_git: true,
      protected: false,
      folder_suggestion: "/elsewhere",
      command: fake.shared || p === "/repos/web" ? null : "whygraph up --add-folder /elsewhere",
      github: fake.shared || p === "/repos/web" ? fake.github : null,
    });
  }
  if (path === "/api/portal/defaults") {
    return reply({ config: {}, secrets: emptySecrets(), no_provider_key: true, ...fake.defaults });
  }
  if (path === "/api/projects" && method === "GET") return reply({ projects: fake.projects });
  if (path === "/api/projects" && method === "POST") {
    if (fake.addError) return reply(fake.addError.body, fake.addError.status);
    const slug = String(body?.source === "github" ? "repo" : "web");
    const created = details(slug, { initialized: false, initialized_at: null, last_scan_at: null });
    fake.projects.push(created);
    fake.config[slug] ??= { config: {}, secrets: emptySecrets() };
    return reply(
      {
        project: created,
        detected: { existing_db: false, managed_hooks: [], detected_agents: [], custom_db_paths: [] },
        import: { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] },
      },
      201,
    );
  }

  const m = /^\/api\/projects\/([^/]+)(\/.*)?$/.exec(path);
  if (m) {
    const [, slug, rest] = m;
    const proj = find(slug);
    if (!proj) return reply({ error: "project not found", code: "not_found" }, 404);
    if (!rest) return reply(details(slug, proj));
    if (rest === "/config" && method === "GET") {
      return reply({
        ...fake.config[slug],
        import: fake.importReport ?? {
          found: false,
          error: null,
          secrets_moved: [],
          dropped: [],
          custom_db_paths: [],
          warnings: [],
        },
      });
    }
    if (rest === "/config" && method === "PUT") {
      const prev = fake.config[slug];
      const secrets = (body?.secrets ?? {}) as { llm?: Record<string, string | null> };
      const llm = { ...(prev.secrets.llm as Json) };
      for (const [k, v] of Object.entries(secrets.llm ?? {})) {
        llm[k] = v === null ? noKey : { set: true, hint: `...${String(v).slice(-4)}` };
      }
      fake.config[slug] = {
        config: (body?.config as Json) ?? prev.config,
        secrets: { ...prev.secrets, llm },
      };
      return reply({
        ...fake.config[slug],
        import: { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] },
        hooks: null,
        hooks_error: null,
      });
    }
    if (rest === "/init") {
      const result = initResult(body ?? {});
      if (result.initialized) proj.initialized = true;
      return reply(result);
    }
    if (rest === "/scans" && method === "POST") return reply({ run_id: body?.trigger === "describe" ? 8 : 7 }, 202);
    if (rest === "/scan-estimate") return reply(fake.estimate);
    if (rest.startsWith("/scans/") && rest.endsWith("/events")) return Promise.resolve(sse(fake.events));
  }
  return reply({ error: `unhandled ${method} ${path}` }, 500);
}

// ---- harness ---------------------------------------------------------------------------

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

const posts = (path: string) => fake.log.filter((c) => c.method === "POST" && c.path === path);

beforeEach(() => {
  fake = {
    setupComplete: true,
    shared: false,
    github: null,
    projects: [],
    config: {},
    defaults: {},
    addError: null,
    init: { needsConfirmation: false },
    events: [],
    estimate: {},
    portChange: null,
    importReport: null,
    log: [],
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

// ---- setup ---------------------------------------------------------------------------------

describe("first-run setup", () => {
  it("redirects every page to /setup until a user exists, then lands on Projects", async () => {
    fake.setupComplete = false;
    const user = userEvent.setup();
    const { router } = mount("/");

    expect(await screen.findByRole("heading", { name: "Welcome to WhyGraph" })).toBeInTheDocument();
    expect(here(router)).toBe("/setup");
    expect(screen.getByText("Local")).toBeInTheDocument();
    expect(screen.getByText(/machine only you use/)).toBeInTheDocument();

    // A blank name is refused client-side; nothing is sent.
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("Enter a name")).toBeInTheDocument();
    expect(posts("/api/portal/setup")).toHaveLength(0);

    await user.type(screen.getByLabelText("Your name"), "  Ada Lovelace ");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(here(router)).toBe("/"));
    expect(posts("/api/portal/setup")[0].body).toEqual({ display_name: "Ada Lovelace" });
    // The empty state is what a fresh portal shows.
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
  });

  it("is a dead end once setup is complete", async () => {
    const { router } = mount("/setup");
    await waitFor(() => expect(here(router)).toBe("/"));
  });
});

// ---- projects list -----------------------------------------------------------------------------

describe("Projects", () => {
  it("shows the empty state with an Add project action", async () => {
    mount("/");
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add project" })).toHaveAttribute("href", "/projects/new");
  });

  it("renders a card per project with its status badge", async () => {
    fake.projects = [
      summary("ready"),
      summary("behind", { stale: { commits_behind: 4 } }),
      summary("fresh", { initialized: false, initialized_at: null, last_scan_at: null }),
      summary("busy", { running_scan: { id: 3, status: "running", trigger: "manual" } }),
      summary("gone", { root_status: "missing" }),
      summary("hub", { source: "github", remote_url: "https://github.com/acme/hub" }),
    ];
    mount("/");
    await screen.findByTestId("project-ready");
    const status = (slug: string) =>
      within(screen.getByTestId(`project-${slug}`)).getByText(
        (_, el) => el?.getAttribute("data-status") !== null && el !== null,
      );
    expect(status("ready")).toHaveTextContent("Ready");
    expect(status("behind")).toHaveTextContent("Stale, 4 commits behind");
    expect(status("fresh")).toHaveTextContent("Not initialized");
    expect(status("busy")).toHaveTextContent("Scanning");
    expect(status("gone")).toHaveTextContent("Folder missing");
    expect(screen.getByTestId("project-hub")).toHaveTextContent("github.com/acme/hub");
    expect(screen.getByTestId("project-hub")).toHaveTextContent("GitHub");

    // An unfinished project resumes the wizard; a finished one opens its home.
    expect(within(screen.getByTestId("project-fresh")).getByRole("link", { name: "Fresh" })).toHaveAttribute(
      "href",
      "/p/fresh/init",
    );
    expect(within(screen.getByTestId("project-ready")).getByRole("link", { name: "Ready" })).toHaveAttribute(
      "href",
      "/p/ready",
    );
  });
});

// ---- add: local --------------------------------------------------------------------------------

describe("Add project - local repo", () => {
  it("lists discovered repos and disables the one already added", async () => {
    mount("/projects/new");
    expect(await screen.findByText("web")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /alpha/ })).toBeDisabled();
    expect(screen.getByText("Already added")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /web/ })).toBeEnabled();
  });

  it("walks the not-shared alert: command, Check again, then adds and continues to Configure", async () => {
    const user = userEvent.setup();
    const { router } = mount("/projects/new");
    await screen.findByText("web");

    await user.type(screen.getByLabelText("Or enter a path"), "/elsewhere/billing");
    await user.click(screen.getByRole("button", { name: "Check" }));

    const alert = await screen.findByTestId("not-shared-alert");
    expect(alert).toHaveTextContent("This folder isn't shared with the portal");
    expect(within(alert).getByText("whygraph up --add-folder /elsewhere")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add project" })).toBeDisabled();

    // The user runs the command; the portal restarts; Check again now succeeds.
    fake.shared = true;
    await user.click(within(alert).getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(screen.queryByTestId("not-shared-alert")).toBeNull());
    expect(await screen.findByText(/Ready to add/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add project" }));
    await waitFor(() => expect(here(router)).toBe("/p/web/init?step=configure"));
    expect(posts("/api/projects")[0].body).toEqual({ source: "local", path: "/elsewhere/billing" });
    expect(await screen.findByRole("heading", { name: /Configure/ })).toBeInTheDocument();
  });

  it("offers the optional token for a GitHub-linked repo and sends it", async () => {
    fake.github = { slug: "acme/web", remote_url: "https://github.com/acme/web" };
    const user = userEvent.setup();
    mount("/projects/new");
    await user.click(await screen.findByRole("radio", { name: /web/ }));
    const token = await screen.findByLabelText(/Linked to github.com\/acme\/web/);
    await user.type(token, "ghp_secret");
    await user.click(screen.getByRole("button", { name: "Add project" }));
    await waitFor(() => expect(posts("/api/projects")).toHaveLength(1));
    expect(posts("/api/projects")[0].body).toEqual({
      source: "local",
      path: "/repos/web",
      token: "ghp_secret",
    });
  });

  it("shows a failed add inline", async () => {
    fake.addError = { status: 409, body: { error: "already registered", code: "duplicate" } };
    const user = userEvent.setup();
    mount("/projects/new");
    await user.click(await screen.findByRole("radio", { name: /web/ }));
    await user.click(await screen.findByRole("button", { name: "Add project" }));
    expect(await screen.findByText("This repository is already a project.")).toBeInTheDocument();
  });
});

// ---- add: GitHub --------------------------------------------------------------------------------

describe("Add project - from GitHub", () => {
  async function openTab() {
    const user = userEvent.setup();
    const ctx = mount("/projects/new");
    await user.click(await screen.findByRole("tab", { name: "From GitHub" }));
    return { user, ...ctx };
  }

  it("validates the URL and requires a token before any request", async () => {
    const { user } = await openTab();
    await user.type(screen.getByLabelText("Repository URL"), "github.com/nope");
    await user.click(screen.getByRole("button", { name: "Add project" }));
    expect(await screen.findByText(/Enter a repository URL like/)).toBeInTheDocument();
    expect(screen.getByText("A token is required to clone")).toBeInTheDocument();
    expect(posts("/api/projects")).toHaveLength(0);
  });

  it.each([
    ["bad_token", 400, /rejected this token/, "Access token"],
    ["no_access", 400, /cannot read the repository/, "Access token"],
    ["not_found", 400, /not found, or not visible/i, "Repository URL"],
  ])("maps %s onto its field", async (code, status, message, field) => {
    fake.addError = { status, body: { error: "probe failed", code } };
    const { user } = await openTab();
    await user.type(screen.getByLabelText("Repository URL"), "https://github.com/acme/secret");
    await user.type(screen.getByLabelText("Access token"), "ghp_x");
    await user.click(screen.getByRole("button", { name: "Add project" }));
    const error = await screen.findByText(message);
    expect(screen.getByLabelText(field)).toHaveAttribute("aria-invalid", "true");
    expect(error).toHaveAttribute("role", "alert");
  });

  it("adds and continues to Configure", async () => {
    const { user, router } = await openTab();
    await user.type(screen.getByLabelText("Repository URL"), "https://github.com/acme/repo");
    await user.type(screen.getByLabelText("Access token"), "ghp_ok");
    await user.click(screen.getByRole("button", { name: "Add project" }));
    await waitFor(() => expect(here(router)).toBe("/p/repo/init?step=configure"));
    expect(posts("/api/projects")[0].body).toEqual({
      source: "github",
      url: "https://github.com/acme/repo",
      token: "ghp_ok",
    });
  });
});

// ---- resume -----------------------------------------------------------------------------------------

describe("resuming the wizard", () => {
  it("an added-but-uninitialized project resumes at Initialize", async () => {
    fake.projects = [summary("alpha", { initialized: false, initialized_at: null, last_scan_at: null })];
    const { router } = mount("/p/alpha/init");
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=initialize"));
    expect(await screen.findByRole("heading", { name: /Initialize/ })).toBeInTheDocument();
  });

  it("an initialized project resumes at the first scan", async () => {
    fake.projects = [summary("alpha", { last_scan_at: null })];
    const { router } = mount("/p/alpha/init");
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=scan"));
    expect(await screen.findByRole("button", { name: "Start first scan" })).toBeInTheDocument();
  });
});

// ---- Configure ----------------------------------------------------------------------------------------

describe("Configure (screen 5)", () => {
  beforeEach(() => {
    fake.projects = [summary("alpha", { initialized: false, initialized_at: null, last_scan_at: null })];
    fake.config.alpha = {
      config: { analyze: { max_diff_chars: 5000 }, scan: { forge: "auto", hooks: ["post-commit"] } },
      secrets: emptySecrets(),
    };
  });

  it("shows the no-key badge for the resolved provider and the four hooks, imported list preserved", async () => {
    fake.projects = [
      summary("alpha", { initialized: false, initialized_at: null, last_scan_at: null, missing_key: "anthropic" }),
    ];
    mount("/p/alpha/init?step=configure");
    const row = await screen.findByTestId("key-anthropic");
    expect(within(row).getByText("no key for anthropic")).toBeInTheDocument();
    expect(within(screen.getByTestId("key-openai")).queryByText(/no key for/)).toBeNull();

    const hooks = screen.getByRole("heading", { name: "Git hooks" }).closest("section")!;
    const boxes = within(hooks).getAllByRole("checkbox");
    expect(boxes).toHaveLength(4);
    expect(within(hooks).getByRole("checkbox", { name: /post-commit/ })).toBeChecked();
    expect(within(hooks).getByRole("checkbox", { name: /post-merge/ })).not.toBeChecked();
  });

  it("lists the keys the whygraph.toml import dropped, with their hints (acceptance #17)", async () => {
    fake.importReport = {
      found: true,
      error: null,
      secrets_moved: ["llm.anthropic.api_key"],
      dropped: [
        { key: "llm.anthropic.base_url", hint: "endpoints are set in the portal, not the repo" },
        { key: "whygraph_db", hint: "the database path is always .whygraph/whygraph.db" },
        { key: "logging.file", hint: "the portal owns logging" },
      ],
      custom_db_paths: [],
      warnings: [],
    };
    mount("/p/alpha/init?step=configure");
    const report = (await screen.findByText("Imported from whygraph.toml")).closest("[role=alert]")!;
    const r = within(report as HTMLElement);
    expect(r.getByText("llm.anthropic.api_key")).toBeInTheDocument();
    const items = r.getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toEqual([
      "llm.anthropic.base_url - endpoints are set in the portal, not the repo",
      "whygraph_db - the database path is always .whygraph/whygraph.db",
      "logging.file - the portal owns logging",
    ]);
  });

  it("hides the hooks for a GitHub clone", async () => {
    fake.projects = [summary("alpha", { source: "github", initialized: false, initialized_at: null })];
    mount("/p/alpha/init?step=configure");
    await screen.findByTestId("config-form");
    await screen.findByText(/Git hooks are not installed in a GitHub clone/);
    expect(screen.queryByRole("heading", { name: "Git hooks" })).toBeNull();
  });

  it("saves the whole layer (unknown keys kept) and secrets write-only, then moves to Initialize", async () => {
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/init?step=configure");
    await screen.findByTestId("config-form");

    await user.selectOptions(screen.getByLabelText("Default model provider"), "anthropic");
    await user.type(screen.getByLabelText("Default model model"), "claude-sonnet-4-5");
    await user.type(screen.getByLabelText("anthropic", { selector: "input" }), "sk-ant-1234");
    await user.click(screen.getByRole("button", { name: "Save and continue" }));

    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=initialize"));
    const put = fake.log.find((c) => c.method === "PUT" && c.path === "/api/projects/alpha/config")!;
    expect(put.body).toEqual({
      config: {
        llm: { model: "anthropic/claude-sonnet-4-5" },
        analyze: { max_diff_chars: 5000 },
        scan: { forge: "auto", hooks: ["post-commit"] },
      },
      secrets: { llm: { anthropic: "sk-ant-1234" } },
    });
    // A secret never travels inside the config dict (rule 4).
    expect(JSON.stringify(put.body!.config)).not.toContain("sk-ant");
  });

  it("continues without a request when nothing changed", async () => {
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/init?step=configure");
    await screen.findByTestId("config-form");
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=initialize"));
    expect(fake.log.some((c) => c.method === "PUT")).toBe(false);
  });

  it("shows a write-only key as set with its hint, and a server rejection", async () => {
    fake.config.alpha.secrets = {
      ...emptySecrets(),
      llm: { ...(emptySecrets().llm as Json), openai: { set: true, hint: "...a1b2" } },
    };
    const user = userEvent.setup();
    mount("/p/alpha/init?step=configure");
    const row = await screen.findByTestId("key-openai");
    expect(within(row).getByText("set ...a1b2")).toBeInTheDocument();
    expect(within(row).getByLabelText("openai", { selector: "input" })).toHaveValue("");

    await user.type(screen.getByLabelText("OpenAI-compatible base URL"), "nonsense");
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(await screen.findByText("Enter an http(s) URL")).toBeInTheDocument();
  });
});

// ---- Initialize ---------------------------------------------------------------------------------------------

describe("Initialize (screen 6)", () => {
  const detected = {
    existing_db: true,
    managed_hooks: ["post-commit"],
    detected_agents: [
      { agent: "claude", file: ".mcp.json", key: "mcpServers.whygraph", shape: "stdio", stale: false, tracked: true },
      { agent: "vscode", file: ".vscode/mcp.json", key: "mcpServers.whygraph", shape: "stdio", stale: true, tracked: false },
    ],
    custom_db_paths: [],
  };

  beforeEach(() => {
    fake.projects = [summary("alpha", { initialized: false, initialized_at: null, last_scan_at: null })];
    fake.init.needsConfirmation = true;
    window.sessionStorage.setItem("whygraph:detected:alpha", JSON.stringify(detected));
  });

  it("pre-selects detected agents, previews each file, and gates Initialize on the tracked-file confirm", async () => {
    const user = userEvent.setup();
    mount("/p/alpha/init?step=initialize");

    expect(await screen.findByTestId("detected-panel")).toHaveTextContent("Existing database");
    expect(screen.getByRole("checkbox", { name: /Claude Code/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /VS Code/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Cursor/ })).not.toBeChecked();

    const tracked = await screen.findByTestId("file-.mcp.json");
    expect(within(tracked).getByTestId("diff")).toHaveTextContent('"type": "http"');
    expect(within(screen.getByTestId("file-.vscode/mcp.json")).getByText("Update")).toBeInTheDocument();

    const initialize = screen.getByRole("button", { name: "Initialize" });
    expect(initialize).toBeDisabled();
    expect(screen.getByTestId("confirm-hint")).toBeInTheDocument();

    await user.click(within(tracked).getByRole("checkbox"));
    await waitFor(() => expect(initialize).toBeEnabled());
    await user.click(initialize);

    expect(await screen.findByTestId("init-done")).toHaveTextContent("Project initialized");
    const call = posts("/api/projects/alpha/init").find((c) => c.body?.dry_run !== true)!;
    expect(call.body).toEqual({
      agents: ["claude", "vscode"],
      agent_actions: {},
      confirm_tracked: [".mcp.json"],
    });
  });

  it("removing a detected entry deselects its agent and sends the remove action", async () => {
    fake.init.needsConfirmation = false;
    const user = userEvent.setup();
    mount("/p/alpha/init?step=initialize");
    const vscode = await screen.findByRole("group", { name: "VS Code / Copilot entry" });
    await user.click(within(vscode).getByRole("button", { name: "Remove entry" }));
    expect(screen.getByRole("checkbox", { name: /VS Code/ })).not.toBeChecked();

    await waitFor(() => {
      const previews = posts("/api/projects/alpha/init").filter((c) => c.body?.dry_run === true);
      expect(previews.at(-1)!.body).toMatchObject({
        agents: ["claude"],
        agent_actions: { vscode: "remove" },
      });
    });
    await user.click(screen.getByRole("button", { name: "Initialize" }));
    await screen.findByTestId("init-done");
    const call = posts("/api/projects/alpha/init").find((c) => c.body?.dry_run !== true)!;
    expect(call.body).toMatchObject({ agents: ["claude"], agent_actions: { vscode: "remove" } });
  });

  it("continues from the done panel to the first scan", async () => {
    fake.init.needsConfirmation = false;
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/init?step=initialize");
    await user.click(await screen.findByRole("button", { name: "Initialize" }));
    await user.click(await screen.findByRole("button", { name: "Continue to first scan" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=scan"));
  });
});

// ---- First scan -------------------------------------------------------------------------------------------------

describe("First scan", () => {
  beforeEach(() => {
    fake.projects = [summary("alpha", { last_scan_at: null })];
    fake.estimate = {
      commits: 120,
      upper_bound: true,
      large_commits: 0,
      model: { provider: "anthropic", model: "claude-haiku-4-5" },
      tokens: {
        input: 500_000,
        output: 60_000,
        input_range: { low: 250_000, high: 750_000 },
        output_range: { low: 30_000, high: 90_000 },
      },
      cost: { usd: 1.2, low: 0.6, high: 1.8, currency: "USD", prices_as_of: "2026-09-01" },
      missing_key: null,
    };
    fake.events = [
      'id: 1\ndata: {"type":"start","phase_total":2}\n\n',
      'id: 2\ndata: {"type":"phase","phase":1,"title":"Structural crawl"}\n\n',
      'id: 3\ndata: {"type":"result","status":"ok"}\n\n',
      'id: 4\nevent: end\ndata: {"type":"end","run_id":7,"status":"ok","summary":null}\n\n',
    ];
  });

  it("runs structure-only, streams progress, then offers Describe now / Later with the estimate", async () => {
    const user = userEvent.setup();
    mount("/p/alpha/init?step=scan");
    await user.click(await screen.findByRole("button", { name: "Start first scan" }));

    // The first scan is a plain manual request; the backend records it as `initial`.
    await waitFor(() => expect(posts("/api/projects/alpha/scans")).toHaveLength(1));
    expect(posts("/api/projects/alpha/scans")[0].body).toEqual({ trigger: "manual" });
    const eventsCall = fake.log.find((c) => c.path === "/api/projects/alpha/scans/7/events");
    expect(eventsCall).toBeTruthy();

    const card = await screen.findByTestId("scan-estimate");
    expect(card).toHaveTextContent("120 commits to describe");
    expect(card).toHaveTextContent("anthropic/claude-haiku-4-5");
    expect(card).toHaveTextContent("~$1.20");

    await user.click(screen.getByRole("button", { name: "Describe now" }));
    await waitFor(() => expect(posts("/api/projects/alpha/scans")).toHaveLength(2));
    expect(posts("/api/projects/alpha/scans")[1].body).toEqual({ trigger: "describe" });
  });

  it("Later leaves for the project home without queuing anything", async () => {
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/init?step=scan");
    await user.click(await screen.findByRole("button", { name: "Start first scan" }));
    await user.click(await screen.findByRole("button", { name: "Later" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha"));
    expect(posts("/api/projects/alpha/scans")).toHaveLength(1);
  });

  it("reports a failed scan with a retry", async () => {
    fake.events = [
      'id: 1\ndata: {"type":"start","phase_total":2}\n\n',
      'id: 2\nevent: end\ndata: {"type":"end","run_id":7,"status":"failed","summary":{"error":"codegraph crashed"}}\n\n',
    ];
    const user = userEvent.setup();
    mount("/p/alpha/init?step=scan");
    await user.click(await screen.findByRole("button", { name: "Start first scan" }));
    const failed = await screen.findByTestId("scan-failed");
    expect(failed).toHaveTextContent("codegraph crashed");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

// ---- port change (acceptance #27) ------------------------------------------------------------

const PORT_CHANGE = {
  port: 9001,
  previous_port: 8765,
  projects: [
    {
      slug: "alpha",
      root: "/repos/alpha",
      previous_port: 8765,
      markers: "rewritten",
      agents: [
        { agent: "claude", file: ".mcp.json", action: "env", hint: "export WHYGRAPH_PORT=9001 for your editor" },
        { agent: "cursor", file: ".cursor/mcp.json", action: "rewritten" },
        {
          agent: "codex",
          file: ".codex/config.toml",
          action: "manual",
          line: 'url = "http://127.0.0.1:9001/mcp/alpha"',
          reason: "tracked by git",
          diff: '-url = "http://127.0.0.1:8765/mcp/alpha"\n+url = "http://127.0.0.1:9001/mcp/alpha"',
        },
      ],
    },
  ],
  unmounted: [{ slug: "away", root: "/repos/away" }],
};

describe("port change", () => {
  beforeEach(() => {
    fake.projects = [summary("alpha"), summary("away", { root_status: "missing" })];
    fake.portChange = PORT_CHANGE;
  });

  it("the Projects banner lists rewritten files, the manual line, env hints and unmounted roots", async () => {
    mount("/");
    const banner = await screen.findByTestId("port-change-banner");
    const b = within(banner);
    expect(b.getByText("The portal now runs on port 9001 (was 8765)")).toBeInTheDocument();
    expect(b.getByText(/updated 2 files; 1 needs a manual edit/)).toBeInTheDocument();
    expect(b.getByText("Portal markers updated (were port 8765).")).toBeInTheDocument();
    expect(b.getByText(/export WHYGRAPH_PORT=9001 for your editor/)).toBeInTheDocument();
    expect(b.getByText(".cursor/mcp.json").closest("li")).toHaveTextContent("updated to the new port");
    const manual = b.getByTestId("port-manual-codex");
    expect(within(manual).getByText('url = "http://127.0.0.1:9001/mcp/alpha"')).toBeInTheDocument();
    expect(manual).toHaveTextContent("tracked by git");
    expect(within(manual).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    const unmounted = b.getByTestId("port-change-unmounted");
    expect(within(unmounted).getByText("/repos/away")).toBeInTheDocument();
  });

  it("is dismissed per port value, and comes back for the next port", async () => {
    const user = userEvent.setup();
    mount("/");
    const banner = await screen.findByTestId("port-change-banner");
    await user.click(within(banner).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("port-change-banner")).toBeNull();
    cleanup();

    mount("/");
    await screen.findByTestId("project-alpha");
    expect(screen.queryByTestId("port-change-banner")).toBeNull();
    cleanup();

    fake.portChange = { ...PORT_CHANGE, port: 9002, previous_port: 9001 };
    mount("/");
    expect(await screen.findByTestId("port-change-banner")).toHaveTextContent("port 9002 (was 9001)");
  });

  it("still shows (and dismisses) when storage throws", async () => {
    const user = userEvent.setup();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    mount("/");
    const banner = await screen.findByTestId("port-change-banner");
    await user.click(within(banner).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("port-change-banner")).toBeNull();
    vi.restoreAllMocks();
  });

  it("the project overview shows that project's slice, with its own dismissal", async () => {
    const user = userEvent.setup();
    fake.projects = [summary("alpha", { port_change: PORT_CHANGE.projects[0] })];
    mount("/p/alpha");
    const notice = await screen.findByTestId("project-port-change");
    expect(within(notice).getByText("The portal moved to port 9001")).toBeInTheDocument();
    expect(within(notice).getByTestId("port-manual-codex")).toHaveTextContent(
      'url = "http://127.0.0.1:9001/mcp/alpha"',
    );
    await user.click(within(notice).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("project-port-change")).toBeNull();
    expect(window.localStorage.getItem("whygraph-port-change-dismissed:project:alpha")).toBe("9001");
  });

  it("an unmounted project says its folder still names the old port", async () => {
    fake.projects = [
      summary("away", { root_status: "missing", port_change: { slug: "away", root: "/repos/away", unmounted: true, port: 9001 } }),
    ];
    mount("/p/away/settings");
    const notice = await screen.findByTestId("project-port-change");
    expect(notice).toHaveTextContent("now runs on port 9001, but this folder was not available");
  });
});
