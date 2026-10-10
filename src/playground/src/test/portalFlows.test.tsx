import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { PROJECT_ACTIONS } from "../lib/permissions";
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
  config: Record<string, { config: Json; secrets: Json; [k: string]: unknown }>;
  defaults: Json;
  addError: { status: number; body: Json } | null;
  init: { needsConfirmation: boolean; scanError?: string };
  events: string[];
  estimate: Json;
  /** `port` of `GET /api/portal/state` (else the port change's, else 8765). */
  port?: number;
  /** `POST .../scans` (not describe) answers this run id (default 7, the first run's). */
  rescanRunId?: number;
  /** `GET .../scans/{id}`: the run's row (its trigger names the wizard's card). */
  runRow: Json | null;
  /** `port_change` of `GET /api/portal/state`. */
  portChange: Json | null;
  /** `import` of `GET .../config` (the whygraph.toml import report). */
  importReport: Json | null;
  log: { method: string; path: string; body: Json | null }[];
}

let fake: Fake;

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
    // The first non-dry Set up queues the first scan (or the runner refuses it).
    ...(!dry && pending.length === 0
      ? fake.init.scanError
        ? { initial_run_id: null, scan_error: fake.init.scanError }
        : { initial_run_id: 7 }
      : {}),
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
      port: fake.port ?? (fake.portChange?.port as number | undefined) ?? 8765,
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
      exists: !p.endsWith("/gone"),
      shared: fake.shared || p === "/repos/web" || p.startsWith("/repos/"),
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
        ...(fake.config[slug] ?? { config: {}, secrets: emptySecrets() }),
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
      const secrets = (body?.secrets ?? {}) as { llm?: Record<string, string | null>; github_token?: string | null };
      const llm = { ...(prev.secrets.llm as Json) };
      for (const [k, v] of Object.entries(secrets.llm ?? {})) {
        llm[k] = v === null ? noKey : { set: true, hint: `...${String(v).slice(-4)}` };
      }
      fake.config[slug] = {
        ...prev,
        config: (body?.config as Json) ?? prev.config,
        secrets: {
          ...prev.secrets,
          llm,
          ...(secrets.github_token !== undefined && {
            github_token: secrets.github_token === null ? noKey : { set: true, hint: "...cret" },
          }),
        },
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
    if (rest === "/scans" && method === "POST") return reply({ run_id: body?.trigger === "describe" ? 8 : (fake.rescanRunId ?? 7) }, 202);
    if (rest === "/scans" && method === "GET") return reply({ runs: [], next: null });
    const run = /^\/scans\/(\d+)$/.exec(rest);
    if (run && method === "GET") {
      return reply(
        fake.runRow ?? {
          id: Number(run[1]),
          kind: "scan",
          trigger: "initial",
          analyze: false,
          status: "running",
          requested_by: null,
          started_at: null,
          finished_at: null,
          summary: null,
        },
      );
    }
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
    runRow: null,
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
      // A local-mode GitHub clone of an older build: listed, with the notice.
      summary("hub", { source: "github", remote_url: "https://github.com/acme/hub", source_supported: false }),
    ];
    mount("/");
    await screen.findByTestId("project-ready");
    const status = (slug: string) =>
      within(screen.getByTestId(`project-${slug}`)).getByText(
        (_, el) => el?.getAttribute("data-status") !== null && el !== null,
      );
    expect(status("ready")).toHaveTextContent("Ready");
    expect(status("behind")).toHaveTextContent("Behind");
    expect(status("fresh")).toHaveTextContent("Needs setup");
    expect(status("busy")).toHaveTextContent("Scanning");
    expect(status("gone")).toHaveTextContent("Folder missing");
    expect(screen.getByTestId("project-hub")).toHaveTextContent("github.com/acme/hub");
    expect(screen.getByTestId("project-hub")).toHaveTextContent("GitHub");
    expect(within(screen.getByTestId("project-hub")).getByTestId("source-unsupported")).toHaveTextContent(
      "This source is no longer supported in local mode - remove the project.",
    );
    expect(within(screen.getByTestId("project-ready")).queryByTestId("source-unsupported")).toBeNull();

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

  it("walks the not-shared alert: command, Check again, then adds and continues to Set up", async () => {
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
    await waitFor(() => expect(here(router)).toBe("/p/web/init?step=setup"));
    expect(posts("/api/projects")[0].body).toEqual({ source: "local", path: "/elsewhere/billing" });
    expect(await screen.findByRole("heading", { name: "Web: Set up" })).toBeInTheDocument();
  });

  it("asks for no GitHub token on Source (Configure asks once, R2) and adds a GitHub-linked repo without one", async () => {
    fake.github = { slug: "acme/web", remote_url: "https://github.com/acme/web" };
    const user = userEvent.setup();
    mount("/projects/new");
    await user.click(await screen.findByRole("radio", { name: /web/ }));
    expect(await screen.findByText(/Ready to add/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Linked to github.com/)).toBeNull();
    expect(screen.queryByPlaceholderText(/GitHub token/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add project" }));
    await waitFor(() => expect(posts("/api/projects")).toHaveLength(1));
    expect(posts("/api/projects")[0].body).toEqual({ source: "local", path: "/repos/web" });
  });

  it("says a missing folder does not exist, not that it is no git repository (BUG-9)", async () => {
    const user = userEvent.setup();
    mount("/projects/new");
    expect(await screen.findByPlaceholderText("Search by name or path")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Or enter a path"), "/repos/gone");
    await user.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("This folder does not exist. Check the path.")).toBeInTheDocument();
    expect(screen.queryByText("This folder is not a git repository.")).toBeNull();
    expect(screen.getByRole("button", { name: "Add project" })).toBeDisabled();
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

// ---- add: no GitHub in local mode ----------------------------------------------------------------

describe("Add project - local mode has no GitHub source", () => {
  it("shows the folder picker only: no tabs, no URL or token field, no GitHub calls", async () => {
    mount("/projects/new");
    await screen.findByText("web");
    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByLabelText("Repository URL")).toBeNull();
    expect(screen.queryByText(/Import from GitHub/)).toBeNull();
    expect(fake.log.some((c) => c.path.startsWith("/api/github"))).toBe(false);
    // One title and two fixed tabs; the local wizard is Source, Set up, Configure.
    expect(screen.getByRole("heading", { name: "Add a project" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "This computer" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "From a platform" })).toBeInTheDocument();
    const steps = within(screen.getByRole("list", { name: "Steps" }));
    expect(steps.getAllByRole("listitem").map((li) => li.textContent).filter(Boolean)).toEqual([
      "1Source",
      "2Set up",
      "3Configure",
    ]);
    expect(screen.getByTestId("wizard-step-compact")).toHaveTextContent("Step 1 of 3 - Source");
  });
});

// ---- resume -----------------------------------------------------------------------------------------

describe("resuming the wizard", () => {
  it("an added-but-uninitialized project resumes at Set up", async () => {
    fake.projects = [summary("alpha", { initialized: false, initialized_at: null, last_scan_at: null })];
    const { router } = mount("/p/alpha/init");
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=setup"));
    expect(await screen.findByRole("heading", { name: "Alpha: Set up" })).toBeInTheDocument();
  });

  it("an initialized project without a scan resumes at Configure, offering the first scan", async () => {
    fake.projects = [summary("alpha", { last_scan_at: null })];
    const { router } = mount("/p/alpha/init");
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=configure"));
    expect(await screen.findByRole("heading", { name: "Alpha: Configure" })).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Start the first scan" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=configure&run=7"));
    expect(posts("/api/projects/alpha/scans")[0].body).toEqual({ trigger: "manual", analyze: false });
  });

  it("Configure before Set up goes back to Set up", async () => {
    fake.projects = [summary("alpha", { initialized: false, initialized_at: null, last_scan_at: null })];
    const { router } = mount("/p/alpha/init?step=configure");
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=setup"));
  });

  it("re-attaches to the running scan after a reload and pins it in the address", async () => {
    fake.projects = [summary("alpha", { last_scan_at: null, running_scan: { id: 7, status: "running", trigger: "initial" } })];
    fake.events = ['id: 1\ndata: {"type":"start","phase_total":2}\n\n'];
    const { router } = mount("/p/alpha/init?step=configure");
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=configure&run=7"));
    expect(await screen.findByTestId("scan-progress")).toBeInTheDocument();
  });
});

// ---- the config form (project settings) -------------------------------------------------------------
// The wizard's Configure step no longer embeds the full form (IMP-3); the form
// itself is the project settings' Models and keys section.

describe("ConfigForm (project settings)", () => {
  beforeEach(() => {
    fake.projects = [summary("alpha")];
    fake.config.alpha = {
      config: { analyze: { max_diff_chars: 5000 }, scan: { forge: "auto", hooks: ["post-commit"] } },
      secrets: emptySecrets(),
    };
  });

  it("shows the no-key badge for the resolved provider and the four hooks, imported list preserved", async () => {
    fake.projects = [summary("alpha", { missing_key: "anthropic" })];
    mount("/p/alpha/settings");
    const row = await screen.findByTestId("key-anthropic");
    expect(within(row).getByTestId("key-warning")).toHaveTextContent("A task uses Anthropic, which has no key.");
    expect(within(screen.getByTestId("key-openai")).queryByTestId("key-warning")).toBeNull();

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
    mount("/p/alpha/settings");
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

  it("hides the hooks for a GitHub clone, with no scheduled-sync note", async () => {
    fake.projects = [summary("alpha", { source: "github" })];
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    expect(screen.queryByRole("heading", { name: "Git hooks" })).toBeNull();
    expect(screen.queryByText(/syncs it on a schedule/)).toBeNull();
    // Local mode keeps the GitHub token for the PR crawl, as its own key card.
    const token = screen.getByTestId("key-github");
    expect(within(token).getByTestId("key-status")).toHaveTextContent("No token");
    expect(within(token).getByRole("button", { name: "Add token" })).toBeInTheDocument();
  });

  it("saves the whole layer (unknown keys kept); a key goes out on its own, write-only", async () => {
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    const puts = () => fake.log.filter((c) => c.method === "PUT" && c.path === "/api/projects/alpha/config");

    // The key card sends its own PUT with only that secret (R3).
    const key = screen.getByTestId("key-anthropic");
    await user.click(within(key).getByRole("button", { name: "Add key" }));
    await user.type(within(key).getByLabelText("New Anthropic key"), "sk-ant-1234");
    await user.click(within(key).getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(puts()[0].body).toEqual({ secrets: { llm: { anthropic: "sk-ant-1234" } } });

    await user.selectOptions(screen.getByLabelText("Default model provider"), "anthropic");
    await user.type(screen.getByLabelText("Default model model"), "claude-sonnet-4-5");
    await user.click(within(screen.getByRole("region", { name: "Models and keys" })).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(puts()).toHaveLength(2));
    expect(puts()[1].body).toEqual({
      config: {
        llm: { model: "anthropic/claude-sonnet-4-5" },
        analyze: { max_diff_chars: 5000 },
        scan: { forge: "auto", hooks: ["post-commit"] },
      },
    });
    // A secret never travels inside the config dict (rule 4).
    expect(JSON.stringify(puts()[1].body)).not.toContain("sk-ant");
  });

  it("sends nothing when nothing changed: Save and Discard wait for an edit", async () => {
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    const models = within(screen.getByRole("region", { name: "Models and keys" }));
    expect(models.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(models.getByRole("button", { name: "Discard" })).toBeDisabled();
    expect(fake.log.some((c) => c.method === "PUT")).toBe(false);
  });

  it("shows a write-only key as set with its hint, and a server rejection", async () => {
    fake.config.alpha.secrets = {
      ...emptySecrets(),
      llm: { ...(emptySecrets().llm as Json), openai: { set: true, hint: "...a1b2" } },
    };
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    const row = await screen.findByTestId("key-openai");
    expect(within(row).getByTestId("key-status")).toHaveTextContent("Set ...a1b2");
    // Write-only: no field holds the key; Replace opens an empty one.
    expect(within(row).queryByRole("textbox")).toBeNull();
    expect(within(row).getByRole("button", { name: "Replace" })).toBeInTheDocument();

    await user.type(screen.getByLabelText("OpenAI-compatible base URL"), "nonsense");
    await user.click(within(screen.getByRole("region", { name: "Models and keys" })).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Enter an http(s) URL")).toBeInTheDocument();
  });
});

// ---- Set up (Initialize) -------------------------------------------------------------------------------------

describe("Set up (Initialize)", () => {
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

  it("pre-selects detected agents, previews each file, gates Set up on the tracked-file confirm, then moves to Configure with the run", async () => {
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/init?step=setup");

    expect(await screen.findByTestId("detected-panel")).toHaveTextContent("Existing database");
    expect(screen.getByRole("checkbox", { name: /Claude Code/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /VS Code/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Cursor/ })).not.toBeChecked();

    const tracked = await screen.findByTestId("file-.mcp.json");
    expect(within(tracked).getByTestId("diff")).toHaveTextContent('"type": "http"');
    expect(within(screen.getByTestId("file-.vscode/mcp.json")).getByText("Update")).toBeInTheDocument();
    // With agents ticked the preview still lists what every Set up writes (BUG-16).
    expect(screen.getByTestId("setup-gitignore")).toBeInTheDocument();
    expect(screen.getByTestId("setup-hooks")).toBeInTheDocument();

    const setUp = screen.getByRole("button", { name: "Set up project" });
    expect(setUp).toBeDisabled();
    expect(screen.getByTestId("confirm-hint")).toBeInTheDocument();

    await user.click(within(tracked).getByRole("checkbox"));
    await waitFor(() => expect(setUp).toBeEnabled());
    await user.click(setUp);

    // The first Set up queued the first scan: straight on to Configure, following it.
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=configure&run=7"));
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
    mount("/p/alpha/init?step=setup");
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
    await user.click(screen.getByRole("button", { name: "Set up project" }));
    await waitFor(() => expect(posts("/api/projects/alpha/init").some((c) => c.body?.dry_run !== true)).toBe(true));
    const call = posts("/api/projects/alpha/init").find((c) => c.body?.dry_run !== true)!;
    expect(call.body).toMatchObject({ agents: ["claude"], agent_actions: { vscode: "remove" } });
  });

  it("names the portal's real port on the agent cards, in plain words (BUG-16, IMP-10)", async () => {
    fake.port = 18765;
    mount("/p/alpha/init?step=setup");
    const claude = (await screen.findByRole("checkbox", { name: /Claude Code/ })).closest("label")!;
    expect(claude).toHaveTextContent("Adds .mcp.json and .claude/ for Claude Code.");
    expect(claude).toHaveTextContent("Connects on port 18765");
    expect(screen.queryByText(/8765\b(?!.)/)).toBeNull();
    expect(screen.queryByText(/unset variable/)).toBeNull();
  });

  it("a refused first scan stays on the done panel with the registry message and Start the first scan", async () => {
    fake.init.needsConfirmation = false;
    fake.init.scanError = "runner_unavailable";
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/init?step=setup");
    await user.click(await screen.findByRole("button", { name: "Set up project" }));
    const refused = await screen.findByTestId("scan-error");
    expect(refused).toHaveTextContent("The scan queue isn't running right now.");
    await user.click(within(refused).getByRole("button", { name: "Start the first scan" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=configure&run=7"));
    expect(posts("/api/projects/alpha/scans")[0].body).toEqual({ trigger: "manual", analyze: false });
  });
});

// ---- Configure ---------------------------------------------------------------------------------------------------

describe("Configure", () => {
  const RUNNING = [
    'id: 1\ndata: {"type":"start","phase_total":2,"phases":["Structural crawl","Author identity"]}\n\n',
    'id: 2\ndata: {"type":"phase","phase":1,"title":"Structural crawl"}\n\n',
    'id: 3\ndata: {"type":"task","name":"git","completed":1240,"total":5300,"description":"git"}\n\n',
  ];
  const DONE = [
    ...RUNNING,
    'id: 4\ndata: {"type":"result","status":"ok"}\n\n',
    'id: 5\nevent: end\ndata: {"type":"end","run_id":7,"status":"ok","summary":{"coverage":{"commits":5300,"described":0,"described_pct":0,"rationale_cards":0}}}\n\n',
  ];

  beforeEach(() => {
    fake.projects = [summary("alpha", { last_scan_at: null })];
    fake.config.alpha = {
      config: {},
      secrets: emptySecrets(),
      effective_keys: { anthropic: "org", openai: "none", openrouter: "none", deepseek: "none" },
      github: { remote: null, token: "none" },
    };
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
    fake.events = DONE;
  });

  it("follows the run in ?run=, then shows the estimate and Describe N commits (~$X)", async () => {
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/init?step=configure&run=7");
    expect(await screen.findByRole("heading", { name: "First scan complete" })).toBeInTheDocument();
    expect(fake.log.some((c) => c.path === "/api/projects/alpha/scans/7/events")).toBe(true);
    // The stepper ticks Configure once the first scan finished (BUG-14).
    const steps = within(screen.getByRole("list", { name: "Steps" }));
    expect(steps.getByText("Configure").closest("li")).toHaveAttribute("data-state", "done");

    const card = await screen.findByTestId("scan-estimate");
    expect(card).toHaveTextContent("120 commits to describe");
    expect(card).toHaveTextContent("anthropic/claude-haiku-4-5");
    expect(card).toHaveTextContent("~$1.20");
    // The choices live in the footer, not the card.
    expect(within(card).queryByRole("button")).toBeNull();
    expect(screen.getByTestId("key-ready")).toHaveTextContent("Uses the Anthropic key from Portal defaults.");

    await user.click(screen.getByRole("button", { name: "Describe 120 commits (~$1.20)" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=configure&run=8"));
    expect(posts("/api/projects/alpha/scans")[0].body).toEqual({ trigger: "describe" });
  });

  it("before the estimate, the describe model is layer-derived prose, not set like a model id", async () => {
    fake.events = RUNNING;
    mount("/p/alpha/init?step=configure&run=7");
    const before = await screen.findByTestId("describe-model");
    await waitFor(() => expect(before).toHaveTextContent("Model: Anthropic, its default model (inherited from Portal defaults)"));
    expect(before.querySelector(".font-mono")).toBeNull();
  });

  it("keeps the describe model's inherited note once the estimate names the model (A4)", async () => {
    // The server's model id in mono, still inherited (the project layer sets none).
    mount("/p/alpha/init?step=configure&run=7");
    await screen.findByTestId("scan-estimate");
    const after = screen.getByTestId("describe-model");
    await waitFor(() => expect(after).toHaveTextContent("Model: anthropic/claude-haiku-4-5 (inherited from Portal defaults)"));
    expect(after.querySelector(".font-mono")).toHaveTextContent("anthropic/claude-haiku-4-5");
  });

  it("drops the inherited note when the project layer picks the describe model", async () => {
    fake.config.alpha.config = { analyze: { provider: "anthropic", model: "claude-haiku-4-5" } };
    mount("/p/alpha/init?step=configure&run=7");
    await screen.findByTestId("scan-estimate");
    const line = screen.getByTestId("describe-model");
    await waitFor(() => expect(line).toHaveTextContent("Model: anthropic/claude-haiku-4-5"));
    expect(line).not.toHaveTextContent("inherited");
  });

  it("while the scan runs: one bar with a status line, the estimate placeholder, Describe disabled", async () => {
    fake.events = RUNNING;
    mount("/p/alpha/init?step=configure&run=7");
    expect(await screen.findByText("Reading git history - 1,240 of 5,300 commits")).toBeInTheDocument();
    const steps = within(screen.getByTestId("scan-steps"));
    expect(steps.getByText("Git history and GitHub")).toBeInTheDocument();
    expect(steps.getByText("Author identities")).toBeInTheDocument();
    expect(screen.queryByText("git")).toBeNull();
    expect(screen.getByRole("link", { name: "Show details" })).toHaveAttribute("href", "/p/alpha/scans/7");
    expect(screen.getByTestId("estimate-pending")).toHaveTextContent(
      "Estimating once the first scan has read the history",
    );
    expect(screen.getByRole("button", { name: "Describe commits" })).toBeDisabled();
    expect(fake.log.some((c) => c.path === "/api/projects/alpha/scan-estimate")).toBe(false);
    // Open project is always there: the scan carries on.
    expect(screen.getByRole("button", { name: "Open project" })).toBeEnabled();
  });

  it("Open project leaves without queuing anything", async () => {
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/init?step=configure&run=7");
    await user.click(await screen.findByRole("button", { name: "Open project" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha"));
    expect(posts("/api/projects/alpha/scans")).toHaveLength(0);
  });

  it("reports a failed scan with a retry", async () => {
    fake.events = [
      'id: 1\ndata: {"type":"start","phase_total":2}\n\n',
      'id: 2\nevent: end\ndata: {"type":"end","run_id":7,"status":"failed","summary":{"error":"codegraph crashed"}}\n\n',
    ];
    const user = userEvent.setup();
    const { router } = mount("/p/alpha/init?step=configure&run=7");
    const failed = await screen.findByTestId("scan-failed");
    expect(failed).toHaveTextContent("codegraph crashed");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(here(router)).toBe("/p/alpha/init?step=configure&run=7"));
    expect(posts("/api/projects/alpha/scans")[0].body).toEqual({ trigger: "manual", analyze: false });
  });

  it("titles a describe run from its row after a reload (BUG-22)", async () => {
    fake.projects = [summary("alpha")];
    fake.runRow = { id: 8, kind: "scan", trigger: "describe", analyze: true, status: "running" };
    fake.events = ['id: 1\ndata: {"type":"start","phase_total":4}\n\n'];
    mount("/p/alpha/init?step=configure&run=8");
    expect(await screen.findByRole("heading", { name: "Writing descriptions" })).toBeInTheDocument();
  });

  it("says No commits yet for an empty history (BUG-14)", async () => {
    fake.estimate = { ...fake.estimate, commits: 0 };
    fake.events = [
      'id: 1\ndata: {"type":"start","phase_total":1}\n\n',
      'id: 2\nevent: end\ndata: {"type":"end","run_id":7,"status":"ok","summary":{"coverage":{"commits":0,"described":0,"described_pct":0,"rationale_cards":0}}}\n\n',
    ];
    mount("/p/alpha/init?step=configure&run=7");
    expect(await screen.findByText("No commits yet. Push some history, then rescan.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Describe/ })).toBeNull();
  });

  it("a missing describe key is information, with an inline key field that saves a project key", async () => {
    fake.config.alpha.effective_keys = { anthropic: "none", openai: "none", openrouter: "none", deepseek: "none" };
    fake.estimate = { ...fake.estimate, missing_key: "anthropic" };
    const user = userEvent.setup();
    mount("/p/alpha/init?step=configure&run=7");
    const missing = await screen.findByTestId("key-missing");
    expect(missing).toHaveTextContent("No Anthropic key yet");
    expect(missing).toHaveTextContent("Descriptions can wait; add one now or later.");
    expect(missing.className).toMatch(/bg-info-soft/);
    await user.type(within(missing).getByLabelText("Anthropic API key"), "sk-ant-9999");
    await user.click(within(missing).getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(fake.log.some((c) => c.method === "PUT")).toBe(true));
    const put = fake.log.find((c) => c.method === "PUT")!;
    expect(put.body).toEqual({ secrets: { llm: { anthropic: "sk-ant-9999" } } });
  });

  it("the GitHub token row saves the token and queues a quick rescan (R2)", async () => {
    fake.config.alpha.github = { remote: "acme/alpha", token: "none" };
    fake.events = RUNNING;
    const user = userEvent.setup();
    mount("/p/alpha/init?step=configure&run=7");
    const row = await screen.findByTestId("github-token-row");
    expect(row).toHaveTextContent("PRs and issues need a GitHub token.");
    await user.type(within(row).getByLabelText("GitHub token"), "ghp_secret");
    await user.click(within(row).getByRole("button", { name: "Save token" }));
    expect(await screen.findByTestId("github-token-saved")).toHaveTextContent(
      "WhyGraph fetches pull requests and issues right after the first scan.",
    );
    const put = fake.log.find((c) => c.method === "PUT")!;
    expect(put.body).toEqual({ secrets: { github_token: "ghp_secret" } });
    expect(posts("/api/projects/alpha/scans").at(-1)!.body).toEqual({ trigger: "manual", analyze: false });
  });

  it("after the first scan, a saved GitHub token says a quick rescan fetches PRs now and links that run (A5)", async () => {
    fake.config.alpha.github = { remote: "acme/alpha", token: "none" };
    fake.rescanRunId = 9;
    const user = userEvent.setup();
    mount("/p/alpha/init?step=configure&run=7");
    expect(await screen.findByRole("heading", { name: "First scan complete" })).toBeInTheDocument();
    // The blurb follows the run: the scan no longer "runs in the background".
    expect(screen.getByText(/The first scan is complete\./)).toBeInTheDocument();
    expect(screen.queryByText(/The first scan runs in the background/)).toBeNull();
    const row = screen.getByTestId("github-token-row");
    expect(row).toHaveTextContent("Add one and WhyGraph fetches them in a quick rescan.");
    await user.type(within(row).getByLabelText("GitHub token"), "ghp_secret");
    await user.click(within(row).getByRole("button", { name: "Save token" }));
    const saved = await screen.findByTestId("github-token-saved");
    // The fake streams the same ended run for the rescan: the row follows it to its end.
    await waitFor(() => expect(saved).toHaveTextContent("Saved. The rescan has fetched pull requests and issues."));
    expect(within(saved).getByTestId("github-token-rescan")).toHaveAttribute("href", "/p/alpha/scans/9");
    expect(fake.log.some((c) => c.path === "/api/projects/alpha/scans/9/events")).toBe(true);
    expect(posts("/api/projects/alpha/scans").at(-1)!.body).toEqual({ trigger: "manual", analyze: false });
  });

  it("names the quick rescan while it runs after the first scan", async () => {
    fake.config.alpha.github = { remote: "acme/alpha", token: "none" };
    fake.rescanRunId = 9;
    const user = userEvent.setup();
    mount("/p/alpha/init?step=configure&run=7");
    expect(await screen.findByRole("heading", { name: "First scan complete" })).toBeInTheDocument();
    fake.events = RUNNING;
    const row = screen.getByTestId("github-token-row");
    await user.type(within(row).getByLabelText("GitHub token"), "ghp_secret");
    await user.click(within(row).getByRole("button", { name: "Save token" }));
    const saved = await screen.findByTestId("github-token-saved");
    expect(saved).toHaveTextContent("Saved. WhyGraph fetches pull requests and issues in a quick rescan now.");
    expect(within(saved).getByRole("link", { name: "Follow the rescan" })).toHaveAttribute("href", "/p/alpha/scans/9");
  });

  it("the GitHub token row has nothing to do when the portal default token applies", async () => {
    fake.config.alpha.github = { remote: "acme/alpha", token: "org" };
    mount("/p/alpha/init?step=configure&run=7");
    expect(await screen.findByTestId("github-token-row")).toHaveTextContent("Using the portal default token");
    expect(screen.queryByLabelText("GitHub token")).toBeNull();
  });

  it("links More settings with the local list", async () => {
    mount("/p/alpha/init?step=configure&run=7");
    expect(await screen.findByRole("link", { name: "More settings (chat model, hooks, limits)" })).toHaveAttribute(
      "href",
      "/p/alpha/settings",
    );
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
