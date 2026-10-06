import { render, screen, waitFor, within } from "@testing-library/react";
import { PROJECT_ACTIONS } from "../lib/permissions";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setBaseUrl } from "../api";
import { hardNavigate } from "../lib/navigation";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

// Production's projects (M2d-2 plan section 4.10) against a fake portal: the
// GitHub App callback page, Import from GitHub, the production wizard, the
// access-lost badge, project and org settings, and deleting an org.

const hard = vi.mocked(hardNavigate);

const BASE = "http://whygraph.localhost:8765";
const ORG = "http://acme.whygraph.localhost:8765";
const INSTALL_URL = "https://github.com/apps/whygraph/installations/new?state=s";
const AUTHORIZE_URL = "https://github.com/login/oauth/authorize?client_id=c&state=s";

type Json = Record<string, unknown>;
type Reply = unknown | { status: number; body: unknown };
type Handler = (body: Json | null, url: URL) => Reply;

let handlers: Record<string, Handler>;
let state: Json;
let log: { method: string; path: string; body: Json | null }[];

const ada = { uid: "u1", display_name: "Ada", email: null, role: null, is_instance_admin: false, github_login: "ada" };

function baseState(over: Json = {}): Json {
  return {
    mode: "production",
    host_kind: "base",
    base_url: BASE,
    setup_complete: true,
    bootstrap_required: false,
    user: ada,
    org: null,
    ...over,
  };
}
function orgState(role: string, over: Json = {}): Json {
  return baseState({ host_kind: "org", org: { slug: "acme", name: "Acme", role }, ...over });
}

const noKey = { set: false, hint: null };
const secrets = () => ({
  llm: { anthropic: noKey, openai: noKey, deepseek: noKey, openrouter: noKey },
  github_token: noKey,
  claude_oauth_token: noKey,
});

function project(slug: string, over: Json = {}): Json {
  return {
    slug,
    name: slug[0].toUpperCase() + slug.slice(1),
    source: "github",
    root: `/data/repos/acme/${slug}`,
    remote_url: `https://github.com/acme/${slug}`,
    initialized: true,
    initialized_at: "2026-10-01T00:00:00Z",
    last_scan_at: "2026-10-02T00:00:00Z",
    created_at: "2026-10-01T00:00:00Z",
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
    github_full_name: `acme/${slug}`,
    installation_account: "acme",
    agents: [],
    missing_key: null,
    mcp_url: null,
    detected: null,
    port_change: null,
    stats: null,
    ...over,
  };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const method = init?.method ?? "GET";
  const body = init?.body ? (JSON.parse(String(init.body)) as Json) : null;
  log.push({ method, path: url.pathname + url.search, body });
  if (url.pathname === "/api/portal/state") return Promise.resolve(json(state));
  const handler = handlers[`${method} ${url.pathname}`];
  if (!handler) return Promise.resolve(json({ error: `unhandled ${method} ${url.pathname}` }, 500));
  const out = handler(body, url);
  if (out && typeof out === "object" && "status" in out && "body" in out) {
    const r = out as { status: number; body: unknown };
    return Promise.resolve(json(r.body, r.status));
  }
  return Promise.resolve(json(out));
}

const calls = (method: string, path: string) => log.filter((c) => c.method === method && c.path === path);

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

const repo = (id: number, full_name: string, over: Json = {}) => ({
  id,
  full_name,
  private: false,
  default_branch: "main",
  imported: false,
  ...over,
});

beforeEach(() => {
  state = orgState("owner");
  log = [];
  handlers = {
    "GET /api/projects": () => ({ projects: [] }),
    "GET /api/portal/defaults": () => ({ config: {}, secrets: secrets(), no_provider_key: false }),
    "POST /api/github/app/authorize": (body) => ({ url: body?.install ? INSTALL_URL : AUTHORIZE_URL }),
    "GET /api/github/installations": () => ({
      installations: [
        { id: 11, account_login: "acme", account_type: "Organization", avatar_url: null, repository_selection: "selected" },
        { id: 12, account_login: "ada", account_type: "User", avatar_url: null, repository_selection: "all" },
      ],
    }),
    "GET /api/github/installations/11/repos": () => ({
      repos: [repo(1, "acme/api"), repo(2, "acme/web", { imported: true, private: true })],
      total_count: 2,
      page: 1,
    }),
  };
  hard.mockClear();
  setBaseUrl(null);
  window.localStorage.clear();
  window.sessionStorage.clear();
  useUi.setState({ paletteOpen: false, navOpen: false });
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })),
  );
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  window.history.replaceState(null, "", "/");
});
afterEach(() => {
  vi.unstubAllGlobals();
  setBaseUrl(null);
});

// ---- the GitHub App's return address ------------------------------------------------------------

describe("GitHub App callback page", () => {
  function visit(query: string) {
    state = baseState();
    window.history.replaceState(null, "", `/auth/github-app${query}`);
    return mount(`/auth/github-app${query}`);
  }
  const reply = (status: number, body: unknown) => {
    handlers["POST /api/github/app/callback"] = () => ({ status, body });
  };
  const posted = () => calls("POST", "/api/github/app/callback");

  it("posts an authorize arrival (code, state, iss), strips the query, then follows return_to", async () => {
    reply(200, { return_to: `${ORG}/projects/new` });
    visit("?code=the-code&state=the-state&iss=https%3A%2F%2Fgithub.com%2Flogin%2Foauth");
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${ORG}/projects/new`));
    expect(window.location.search).toBe("");
    expect(posted()).toHaveLength(1);
    expect(posted()[0].body).toEqual({
      code: "the-code",
      state: "the-state",
      iss: "https://github.com/login/oauth",
    });
  });

  it("posts an install arrival with a numeric installation_id", async () => {
    reply(200, { return_to: `${ORG}/projects/new` });
    visit("?code=c&state=s&installation_id=42&setup_action=install");
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${ORG}/projects/new`));
    expect(posted()[0].body).toEqual({ code: "c", state: "s", installation_id: 42, setup_action: "install" });
  });

  it("says a request went to the GitHub organization's owners, with a way back", async () => {
    reply(200, { requested: true, return_to: `${ORG}/projects/new` });
    visit("?setup_action=request&state=s");
    const done = await screen.findByTestId("github-app-callback-requested");
    expect(done).toHaveTextContent("Your request was sent to the GitHub organization's owners.");
    expect(screen.getByRole("link", { name: "Back to the import page" })).toHaveAttribute(
      "href",
      `${ORG}/projects/new`,
    );
    expect(posted()[0].body).toEqual({ state: "s", setup_action: "request" });
    expect(hard).not.toHaveBeenCalled();
  });

  it("a request the portal did not start links to the org picker", async () => {
    reply(200, { requested: true, return_to: null });
    visit("?setup_action=request");
    await screen.findByTestId("github-app-callback-requested");
    expect(screen.getByRole("link", { name: "Back to your organizations" })).toHaveAttribute("href", "/orgs");
  });

  it("says the authorization was cancelled on GitHub's access_denied (no POST)", async () => {
    visit("?error=access_denied&state=s");
    expect(await screen.findByTestId("github-app-callback-error")).toHaveTextContent(
      "The GitHub authorization was cancelled.",
    );
    expect(window.location.search).toBe("");
    expect(posted()).toHaveLength(0);
  });

  it("an incomplete return address posts nothing", async () => {
    visit("?state=s");
    expect(await screen.findByTestId("github-app-callback-error")).toHaveTextContent(/incomplete/);
    expect(posted()).toHaveLength(0);
  });

  it.each([
    ["start_from_portal", 409, /Open your organization in WhyGraph and choose Import from GitHub/],
    ["github_account_mismatch", 403, /a different GitHub account than the one you signed in with/],
    ["github_required", 403, /needs an account that signs in with GitHub/],
    ["oauth_state", 400, /expired or was started in another browser/],
    ["github_auth_failed", 400, /GitHub did not confirm the authorization/],
    ["github_unavailable", 502, /GitHub could not be reached/],
    ["github_app_not_configured", 503, /no GitHub App configured/],
    ["forbidden", 403, /no longer a member of that organization/],
  ])("renders %s", async (code, status, text) => {
    reply(status, { error: "you are no longer a member of that organization", code });
    visit("?code=c&state=s&installation_id=1&setup_action=install");
    expect(await screen.findByTestId("github-app-callback-error")).toHaveTextContent(text);
    expect(hard).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Back to your organizations" })).toBeInTheDocument();
  });

  it("is a base-host page: an org host hands it over, query included", async () => {
    state = orgState("owner");
    mount("/auth/github-app?code=c&state=s");
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${BASE}/auth/github-app?code=c&state=s`));
  });
});

// ---- Import from GitHub -------------------------------------------------------------------------

describe("Import from GitHub", () => {
  it("offers Connect GitHub without a user token, and goes to GitHub's authorize URL", async () => {
    handlers["GET /api/github/installations"] = () => ({
      status: 401,
      body: { error: "connect GitHub first", code: "github_authorization_required" },
    });
    const user = userEvent.setup();
    mount("/projects/new");
    const connect = await screen.findByTestId("github-connect");
    await user.click(within(connect).getByRole("button", { name: "Connect GitHub" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(AUTHORIZE_URL));
    expect(calls("POST", "/api/github/app/authorize")[0].body).toEqual({ install: false });
  });

  it("lists the installations; Install / configure goes to the app's install page", async () => {
    const user = userEvent.setup();
    mount("/projects/new");
    const accounts = await screen.findByRole("radiogroup", { name: "GitHub accounts" });
    expect(within(accounts).getByRole("radio", { name: "acme" })).toHaveAttribute("aria-checked", "true");
    expect(within(accounts).getByRole("radio", { name: "ada" })).toHaveAttribute("aria-checked", "false");
    await user.click(screen.getByRole("button", { name: "Install / configure on GitHub" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(INSTALL_URL));
    expect(calls("POST", "/api/github/app/authorize")[0].body).toEqual({ install: true });
  });

  it("offers Install on GitHub when the app is installed nowhere the user can see", async () => {
    handlers["GET /api/github/installations"] = () => ({ installations: [] });
    mount("/projects/new");
    expect(await screen.findByText(/not installed on any GitHub account you can see/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install on GitHub" })).toBeInTheDocument();
  });

  it("disables an imported repo and switches installations", async () => {
    handlers["GET /api/github/installations/12/repos"] = () => ({
      repos: [repo(5, "ada/notes")],
      total_count: 1,
      page: 1,
    });
    const user = userEvent.setup();
    mount("/projects/new");
    const web = await screen.findByTestId("repo-acme/web");
    expect(within(web).getByRole("button", { name: "acme/web is already imported" })).toBeDisabled();
    expect(web).toHaveTextContent("Private");
    expect(within(screen.getByTestId("repo-acme/api")).getByRole("button", { name: "Import acme/api" })).toBeEnabled();

    await user.click(screen.getByRole("radio", { name: "ada" }));
    expect(await screen.findByTestId("repo-ada/notes")).toBeInTheDocument();
    expect(screen.queryByTestId("repo-acme/api")).toBeNull();
  });

  it("loads more pages and filters what is loaded on the client", async () => {
    const page = (n: number) =>
      Array.from({ length: 100 }, (_, i) => repo(n * 1000 + i, `acme/repo-${n}-${String(i).padStart(3, "0")}`));
    handlers["GET /api/github/installations/11/repos"] = (_, url) => {
      const n = Number(url.searchParams.get("page"));
      return n === 1
        ? { repos: page(1), total_count: 150, page: 1 }
        : { repos: page(2).slice(0, 50), total_count: 150, page: 2 };
    };
    const user = userEvent.setup();
    mount("/projects/new");
    await screen.findByTestId("repo-acme/repo-1-000");
    expect(screen.getByText("100 of 150 loaded")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByTestId("repo-acme/repo-2-049")).toBeInTheDocument();
    expect(screen.getByText("150 of 150 loaded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    expect(calls("GET", "/api/github/installations/11/repos?page=2")).toHaveLength(1);

    await user.type(screen.getByLabelText("Filter repositories"), "repo-2-04");
    const list = screen.getByRole("list", { name: "Repositories" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(10);
    // The filter never asks the server.
    expect(log.filter((c) => c.path.includes("/repos?"))).toHaveLength(2);
  });

  it("imports, then continues to Configure in a three-step production wizard", async () => {
    handlers["POST /api/projects"] = () => ({
      status: 201,
      body: { project: project("api"), detected: null, import: { found: false, secrets_moved: [], dropped: [] } },
    });
    handlers["GET /api/projects/api"] = () => project("api");
    handlers["GET /api/projects/api/config"] = () => ({
      config: { scan: { forge: "auto" } },
      secrets: secrets(),
      import: { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] },
    });
    const user = userEvent.setup();
    const router = mount("/projects/new");
    const steps = within(await screen.findByRole("list", { name: "Steps" }));
    expect(steps.queryByText("Initialize")).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Import acme/api" }));
    await waitFor(() => expect(here(router)).toBe("/p/api/init?step=configure"));
    expect(calls("POST", "/api/projects")[0].body).toEqual({ source: "github", installation_id: 11, repo_id: 1 });
    await screen.findByTestId("config-form");
    const wizard = within(screen.getByRole("list", { name: "Steps" }));
    expect(wizard.getAllByRole("listitem").map((li) => li.textContent).filter(Boolean)).toEqual([
      "Source",
      "2Configure",
      "3First scan",
    ]);
  });

  it.each([
    ["no_access", 404, /not available to you through this installation/],
    ["tracked_whygraph_state", 422, /tracks WhyGraph's own state/],
    ["duplicate", 409, /already a project in this organization/],
    ["source_not_allowed", 403, /not supported here/],
    ["throttled", 429, /Too many imports/],
  ])("shows a refused import (%s)", async (code, status, text) => {
    handlers["POST /api/projects"] = () => ({ status, body: { error: "refused", code } });
    const user = userEvent.setup();
    const router = mount("/projects/new");
    await user.click(await screen.findByRole("button", { name: "Import acme/api" }));
    expect(await screen.findByTestId("import-error")).toHaveTextContent(text);
    expect(here(router)).toBe("/projects/new");
  });

  it("an import that finds the token gone asks to connect again", async () => {
    handlers["POST /api/projects"] = () => ({
      status: 401,
      body: { error: "connect GitHub first", code: "github_authorization_required" },
    });
    const user = userEvent.setup();
    mount("/projects/new");
    await user.click(await screen.findByRole("button", { name: "Import acme/api" }));
    expect(await screen.findByTestId("github-connect")).toBeInTheDocument();
  });

  it("shows why a password account cannot import", async () => {
    handlers["GET /api/github/installations"] = () => ({
      status: 403,
      body: { error: "x", code: "github_required" },
    });
    mount("/projects/new");
    expect(await screen.findByTestId("github-error")).toHaveTextContent(
      "Importing from GitHub needs an account that signs in with GitHub.",
    );
  });

  it("is for owners and admins: a member goes back to Projects, which has no New project", async () => {
    handlers["GET /api/projects"] = () => ({ projects: [project("api")] });
    state = orgState("member");
    const router = mount("/projects/new");
    await waitFor(() => expect(here(router)).toBe("/"));
    await screen.findByTestId("project-api");
    expect(screen.queryByRole("link", { name: /New project/ })).toBeNull();
    expect(log.some((c) => c.path.startsWith("/api/github"))).toBe(false);
  });

  it("gives an admin New project, and an empty org Import from GitHub", async () => {
    state = orgState("admin");
    mount("/");
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    expect(screen.queryByText(/next release/)).toBeNull();
    expect(screen.getByRole("link", { name: "Import from GitHub" })).toHaveAttribute("href", "/projects/new");
  });
});

// ---- the production wizard and project settings ------------------------------------------------

describe("production project pages", () => {
  beforeEach(() => {
    handlers["GET /api/projects"] = () => ({ projects: [project("api")] });
    handlers["GET /api/projects/api"] = () => project("api");
    handlers["GET /api/projects/api/scans"] = () => ({ runs: [] });
    handlers["GET /api/projects/api/config"] = () => ({
      config: { scan: { forge: "auto" } },
      secrets: secrets(),
      import: { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] },
    });
    handlers["PUT /api/projects/api/config"] = (body) => ({
      config: body?.config ?? {},
      secrets: secrets(),
      import: { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] },
    });
  });

  it("Configure has no hooks and no GitHub token, keeps the forge toggle, and continues to the first scan", async () => {
    const user = userEvent.setup();
    const router = mount("/p/api/init?step=configure");
    const form = await screen.findByTestId("config-form");
    expect(within(form).queryByRole("heading", { name: "Git hooks" })).toBeNull();
    expect(within(form).queryByLabelText("GitHub token")).toBeNull();
    expect(within(form).queryByText(/syncs it on a schedule/)).toBeNull();
    const forge = within(form).getByRole("switch", { name: "Fetch pull requests and issues from GitHub" });
    expect(forge).toBeChecked();

    await user.type(within(form).getByLabelText("anthropic"), "sk-ant-1234");
    await user.click(within(form).getByRole("button", { name: "Save and continue" }));
    await waitFor(() => expect(here(router)).toBe("/p/api/init?step=scan"));
    const put = calls("PUT", "/api/projects/api/config")[0].body!;
    expect(put.secrets).toEqual({ llm: { anthropic: "sk-ant-1234" } });
  });

  it("sends ?step=initialize on to the first scan", async () => {
    const router = mount("/p/api/init?step=initialize");
    await waitFor(() => expect(here(router)).toBe("/p/api/init?step=scan"));
    expect(calls("POST", "/api/projects/api/init")).toHaveLength(0);
  });

  it("settings show the repository and installation, no Sync now, no Agents, the production Danger zone", async () => {
    mount("/p/api/settings");
    const repoRow = await screen.findByTestId("github-repo");
    expect(repoRow).toHaveTextContent("acme/api");
    expect(within(repoRow).getByRole("link", { name: "acme/api" })).toHaveAttribute(
      "href",
      "https://github.com/acme/api",
    );
    expect(repoRow).toHaveTextContent("Installation");
    expect(repoRow).toHaveTextContent("acme");
    expect(screen.queryByRole("button", { name: "Sync now" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Agents" })).toBeNull();
    expect(screen.getByRole("region", { name: "Danger zone" })).toHaveTextContent(
      "Removes the server copy and every scan.",
    );
    expect(screen.queryByText(/removes its git hooks/)).toBeNull();
  });

  it("removing names the server copy, never hooks or agent files, and needs the name typed", async () => {
    handlers["DELETE /api/projects/api"] = () => ({
      removed: "api",
      hooks: null,
      agent_files: [],
      checkout_deleted: true,
      warnings: [],
    });
    const user = userEvent.setup();
    mount("/p/api/settings");
    await user.click(await screen.findByRole("button", { name: "Remove project" }));
    const dialog = await screen.findByTestId("remove-dialog");
    expect(dialog).toHaveTextContent("This removes the server copy and every scan.");
    expect(dialog).toHaveTextContent("/data/repos/acme/api");
    expect(dialog).toHaveTextContent("The repository on GitHub; nothing is changed there.");
    expect(dialog).not.toHaveTextContent(/git hooks|portal\.\*|MCP entries|checkout/);
    expect(within(dialog).queryByRole("checkbox")).toBeNull();

    const remove = within(dialog).getByRole("button", { name: "Remove project" });
    expect(remove).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/to confirm deleting the server copy/), "Api");
    expect(remove).toBeEnabled();
    await user.click(remove);
    await waitFor(() => expect(calls("DELETE", "/api/projects/api")).toHaveLength(1));
    expect(calls("DELETE", "/api/projects/api")[0].body).toEqual({
      strip_agent_entries: false,
      confirm_tracked: [],
      confirm_name: "Api",
    });
    expect(await screen.findByTestId("remove-done")).toHaveTextContent("The server copy was deleted.");
  });
});

// ---- access lost ---------------------------------------------------------------------------------

describe("access-lost badge", () => {
  const lost = (reason: string) => project("api", { access_lost: true, access_lost_reason: reason });

  it("names the reason and offers Reconnect on GitHub to an admin", async () => {
    state = orgState("admin");
    handlers["GET /api/projects"] = () => ({ projects: [lost("no_access"), project("web")] });
    const user = userEvent.setup();
    mount("/");
    const badge = await within(await screen.findByTestId("project-api")).findByTestId("access-lost");
    expect(badge).toHaveTextContent("Access lost");
    expect(badge).toHaveTextContent("The WhyGraph app can no longer read this repository.");
    expect(within(screen.getByTestId("project-web")).queryByTestId("access-lost")).toBeNull();
    await user.click(within(badge).getByRole("button", { name: "Reconnect on GitHub" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(INSTALL_URL));
    expect(calls("POST", "/api/github/app/authorize")[0].body).toEqual({ install: true });
  });

  it.each([
    ["repo_deleted", "The repository was deleted on GitHub."],
    ["git_access_denied", "GitHub refused git access to this repository."],
  ])("says %s", async (reason, text) => {
    handlers["GET /api/projects"] = () => ({ projects: [lost(reason)] });
    mount("/");
    expect(await screen.findByTestId("access-lost")).toHaveTextContent(text);
  });

  it("offers no reconnect for a repo tracking WhyGraph's state, nor to a member", async () => {
    handlers["GET /api/projects"] = () => ({ projects: [lost("tracked_whygraph_state")] });
    mount("/");
    const badge = await screen.findByTestId("access-lost");
    expect(badge).toHaveTextContent(".whygraph/ or .codegraph/");
    expect(within(badge).queryByRole("button", { name: "Reconnect on GitHub" })).toBeNull();

    document.body.innerHTML = "";
    state = orgState("member");
    handlers["GET /api/projects"] = () => ({ projects: [lost("no_access")] });
    mount("/");
    const memberBadge = await screen.findByTestId("access-lost");
    expect(within(memberBadge).queryByRole("button", { name: "Reconnect on GitHub" })).toBeNull();
  });

  it("keeps the project readable but its Scan now disabled", async () => {
    handlers["GET /api/projects"] = () => ({ projects: [lost("no_access")] });
    handlers["GET /api/projects/api"] = () => lost("no_access");
    handlers["GET /api/projects/api/scans"] = () => ({ runs: [] });
    handlers["GET /api/projects/api/scan-estimate"] = () => ({ status: 404, body: { error: "x" } });
    mount("/p/api");
    await screen.findByTestId("access-lost");
    expect(screen.getAllByRole("button", { name: "Rescan" })[0]).toBeDisabled();
    expect(screen.getByRole("link", { name: "Open Explorer" })).toBeInTheDocument();
  });
});

// ---- deleting the org ------------------------------------------------------------------------------

describe("delete organization", () => {
  beforeEach(() => {
    handlers["GET /api/projects"] = () => ({ projects: [project("api"), project("web")] });
  });

  async function openDialog() {
    const user = userEvent.setup();
    mount("/settings");
    const zone = await screen.findByTestId("org-danger-zone");
    await user.click(within(zone).getByRole("button", { name: "Delete organization" }));
    const dialog = await screen.findByTestId("delete-org-dialog");
    return { user, dialog };
  }

  it("is for owners only", async () => {
    state = orgState("admin");
    mount("/settings");
    await screen.findByTestId("config-form");
    expect(screen.queryByTestId("org-danger-zone")).toBeNull();
  });

  it("needs the slug typed, says how many projects go, then sends the owner to the picker", async () => {
    handlers["DELETE /api/org"] = () => ({ deleted: "acme", projects: 2 });
    const { user, dialog } = await openDialog();
    await waitFor(() => expect(dialog).toHaveTextContent("Its 2 projects are deleted with it."));
    const confirm = within(dialog).getByRole("button", { name: "Delete organization" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/to confirm/), "acm");
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/to confirm/), "e");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${BASE}/orgs`));
    expect(calls("DELETE", "/api/org")[0].body).toEqual({ confirm_slug: "acme" });
  });

  it("asks to retry while a sync is finishing (409 busy)", async () => {
    handlers["DELETE /api/org"] = () => ({ status: 409, body: { error: "a sync is fetching", code: "busy" } });
    const { user, dialog } = await openDialog();
    await user.type(within(dialog).getByLabelText(/to confirm/), "acme");
    await user.click(within(dialog).getByRole("button", { name: "Delete organization" }));
    expect(await screen.findByTestId("delete-org-error")).toHaveTextContent(
      "A sync is finishing - try again in a minute.",
    );
    expect(hard).not.toHaveBeenCalled();
    // Still open, still confirmable: a retry is one click.
    expect(within(dialog).getByRole("button", { name: "Delete organization" })).toBeEnabled();
  });
});
