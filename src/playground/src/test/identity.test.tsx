import { render, screen, waitFor, within } from "@testing-library/react";
import { PROJECT_ACTIONS, can } from "../lib/permissions";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { portalApi, setBaseUrl } from "../api";
import { RationaleTab } from "../components/RationaleTab";
import { authMessage } from "../lib/authErrors";
import { isSafeNext, signInUrl } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";
import { ProjectProvider } from "../lib/project";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { ApiError } from "../api";
import { useUi } from "../store";
import { formatResetsAt } from "../lib/usageRange";

// `hardNavigate` is mocked globally (src/test/setup.ts); redirect tests wait for
// the spy rather than for the router, because a hard navigation never resolves.
const hard = vi.mocked(hardNavigate);

const BASE = "http://whygraph.localhost:8765";
const ORG = "http://acme.whygraph.localhost:8765";

type Handler = (method: string, body: unknown) => Response;

interface Fake {
  state: Record<string, unknown>;
  orgs: { slug: string; name: string; role: string; url: string; new?: boolean }[];
  login: { status: number; body: unknown };
  calls: { path: string; method: string; body: unknown; search: string; headers: Record<string, string> }[];
  // Per-test routes, matched by exact path before the fixed ones.
  routes: Record<string, Handler>;
}
let fake: Fake;

const ada = { uid: "u1", display_name: "Ada", email: "ada@example.com", role: null, is_instance_admin: false };
const adminAda = { ...ada, is_instance_admin: true };

function baseState(over: Record<string, unknown> = {}) {
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
function orgState(role: string | null, over: Record<string, unknown> = {}) {
  return baseState({
    host_kind: "org",
    org: role ? { slug: "acme", name: "Acme", role } : null,
    ...over,
  });
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const PERMISSIONS: Record<string, string[]> = {
  admin: PROJECT_ACTIONS,
  contributor: ["project.read", "project.chat", "project.scan"],
  viewer: ["project.read"],
};
function myRole(): "admin" | "contributor" | "viewer" {
  const role = (fake.state.org as { role?: string } | null)?.role;
  return role === "member" ? "contributor" : role === "reader" ? "viewer" : "admin";
}

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
    // The project role follows the org role (as the server's `effective_project_role` does).
    my_role: myRole(),
    permissions: PERMISSIONS[myRole()],
    stale: null,
    agents: [],
    missing_key: null,
    mcp_url: null,
    stats: null,
  };
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const path = url.pathname;
  let body: unknown = undefined;
  try {
    body = init?.body ? JSON.parse(String(init.body)) : undefined;
  } catch {
    body = init?.body;
  }
  fake.calls.push({
    path,
    method: init?.method ?? "GET",
    body,
    search: url.search,
    headers: (init?.headers ?? {}) as Record<string, string>,
  });
  const route = fake.routes[path];
  if (route) return Promise.resolve(route(init?.method ?? "GET", body));
  if (path === "/api/portal/state") return Promise.resolve(json(fake.state));
  if (path === "/api/account/orgs") return Promise.resolve(json(fake.orgs));
  if (path === "/api/auth/login") return Promise.resolve(json(fake.login.body, fake.login.status));
  if (path === "/api/auth/reset") return Promise.resolve(json({ redirect: `${BASE}/orgs` }));
  if (path === "/api/projects") return Promise.resolve(json({ projects: [project("alpha")] }));
  if (path === "/api/projects/alpha") return Promise.resolve(json(project("alpha")));
  if (path === "/api/projects/alpha/scans") return Promise.resolve(json({ runs: [] }));
  if (path === "/api/projects/alpha/chat/sessions") return Promise.resolve(json([]));
  if (path === "/api/projects/alpha/node/rationale") return Promise.resolve(json({ status: "missing" }));
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
  return router;
}
const where = (r: ReturnType<typeof mount>) => r.state.location.pathname;

beforeEach(() => {
  fake = {
    state: baseState(),
    orgs: [],
    login: { status: 401, body: { error: "bad", code: "bad_credentials" } },
    calls: [],
    routes: {},
  };
  hard.mockClear();
  setBaseUrl(null);
  window.localStorage.clear();
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

// ---- the router gate: production, base host --------------------------------------

describe("router gate - production base host", () => {
  it("sends everything to /setup while the bootstrap is pending", async () => {
    fake.state = baseState({ bootstrap_required: true, user: null });
    const router = mount("/signin");
    await screen.findByLabelText("Bootstrap secret");
    expect(where(router)).toBe("/setup");
  });

  it("sends /setup to / (then the picker) once the bootstrap is done", async () => {
    const router = mount("/setup");
    await waitFor(() => expect(where(router)).toBe("/orgs"));
  });

  it("sends a signed-out visitor to /signin from anywhere else", async () => {
    fake.state = baseState({ user: null });
    for (const path of ["/", "/orgs", "/admin", "/account", "/p/alpha"]) {
      const router = mount(path);
      await waitFor(() => expect(where(router)).toBe("/signin"));
      screen.getByRole("heading", { name: "Sign in" });
      document.body.innerHTML = "";
    }
  });

  it("lets a signed-out visitor open GitHub's return address", async () => {
    fake.state = baseState({ user: null });
    const router = mount("/auth/github?error=access_denied&state=s");
    await screen.findByTestId("github-callback-error");
    expect(where(router)).toBe("/auth/github");
  });

  it("sends / to the picker when signed in", async () => {
    const router = mount("/");
    await waitFor(() => expect(where(router)).toBe("/orgs"));
  });

  it("has no /register any more (signed out: sign-in; signed in: not found in the base chrome)", async () => {
    fake.state = baseState({ user: null });
    const out = mount("/register");
    await waitFor(() => expect(where(out)).toBe("/signin"));
    document.body.innerHTML = "";
    fake.state = baseState();
    const signedIn = mount("/register");
    await screen.findByRole("heading", { name: "Page not found" });
    expect(where(signedIn)).toBe("/register");
    expect(screen.getByRole("link", { name: "Your organizations" })).toHaveAttribute("href", "/orgs");
    expect(screen.getByRole("navigation", { name: "Account" })).toBeInTheDocument();
    expect(fake.calls.some((c) => c.path === "/api/projects")).toBe(false);
  });

  it("keeps /admin for instance admins", async () => {
    const router = mount("/admin");
    await waitFor(() => expect(where(router)).toBe("/orgs"));

    document.body.innerHTML = "";
    fake.state = baseState({ user: adminAda });
    const admin = mount("/admin");
    await waitFor(() => expect(where(admin)).toBe("/admin"));
  });

  it("sends the org tree and legacy paths to /", async () => {
    fake.state = baseState({ user: adminAda });
    for (const path of ["/settings", "/projects/new", "/explorer", "/chat", "/p/alpha/explorer"]) {
      const router = mount(path);
      await waitFor(() => expect(where(router)).toBe("/orgs"));
      document.body.innerHTML = "";
    }
  });

  it("signed in on /signin without next goes to the picker", async () => {
    const router = mount("/signin");
    await waitFor(() => expect(where(router)).toBe("/orgs"));
  });

  it("signed in on /signin with a valid next and reauth shows the session-not-received page (no loop)", async () => {
    const router = mount(`/signin?next=${encodeURIComponent(`${ORG}/p/alpha`)}&reauth=1`);
    await screen.findByRole("heading", { name: "Session not received" });
    expect(where(router)).toBe("/signin");
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /acme\.whygraph\.localhost:8765 again/ })).toHaveAttribute(
      "href",
      `${ORG}/p/alpha`,
    );
    expect(hard).not.toHaveBeenCalled();
  });

  it("signed in with a valid next and no reauth goes back once, then says the session did not arrive", async () => {
    window.sessionStorage.clear();
    const next = `${ORG}/p/alpha`;
    mount(`/signin?next=${encodeURIComponent(next)}`);
    await waitFor(() => expect(hard).toHaveBeenCalledWith(next));
    expect(screen.queryByRole("heading", { name: "Session not received" })).toBeNull();
    document.body.innerHTML = "";
    hard.mockClear();
    // The org host sent the browser straight back: the cookie is not reaching it.
    mount(`/signin?next=${encodeURIComponent(next)}`);
    await screen.findByRole("heading", { name: "Session not received" });
    expect(hard).not.toHaveBeenCalled();
    window.sessionStorage.clear();
  });

  it("an unsafe next is ignored (picker, not the page)", async () => {
    const router = mount(`/signin?next=${encodeURIComponent("https://evil.example/")}`);
    await waitFor(() => expect(where(router)).toBe("/orgs"));
  });

  it("renders its pages without the app shell (no sidebar, no /api/projects)", async () => {
    mount("/orgs");
    await screen.findByRole("heading", { name: "Your organizations" });
    expect(fake.calls.some((c) => c.path === "/api/projects")).toBe(false);
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
  });
});

// ---- the router gate: production, org host ----------------------------------------

describe("router gate - production org host", () => {
  it("redirects a signed-out visitor to the base sign-in with the org URL as next", async () => {
    fake.state = orgState("owner", { user: null, org: null });
    mount("/p/alpha");
    await waitFor(() => expect(hard).toHaveBeenCalled());
    const url = new URL(hard.mock.calls[0][0]);
    expect(url.origin).toBe(BASE);
    expect(url.pathname).toBe("/signin");
    expect(url.searchParams.get("next")).toContain("/p/alpha");
  });

  it("shows the no-access page to a signed-in non-member", async () => {
    fake.state = orgState(null);
    mount("/");
    await screen.findByRole("heading", { name: "No access to this organization" });
    expect(screen.getByText(/is not a member of/)).toBeInTheDocument();
    expect(screen.getByTestId("join-hint")).toHaveTextContent("Ask an owner to add");
    expect(screen.getByRole("link", { name: "Your organizations" })).toHaveAttribute("href", `${BASE}/orgs`);
  });

  it("hands base-only paths to the base host", async () => {
    fake.state = orgState("owner");
    mount("/account");
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${BASE}/account`));
  });

  it("renders the org tree for a member", async () => {
    fake.state = orgState("member");
    const router = mount("/");
    await screen.findByText("Project alpha");
    expect(where(router)).toBe("/");
    expect(hard).not.toHaveBeenCalled();
    expect(screen.queryByTestId("reader-banner")).toBeNull();
    // Production: no way to add a project, no MCP snippet.
    expect(screen.queryByRole("link", { name: /new project|add project/i })).toBeNull();
  });
});

describe("local mode", () => {
  it("never hard-navigates or touches the base URL", async () => {
    fake.state = { mode: "local", setup_complete: true, user: { uid: "u", display_name: "T", role: "owner" } };
    const router = mount("/");
    await screen.findByText("Project alpha");
    expect(where(router)).toBe("/");
    expect(hard).not.toHaveBeenCalled();
  });
});

// ---- the 401 redirect --------------------------------------------------------------

describe("login_required redirect", () => {
  it("hard-navigates to <base>/signin?next=<href> on 401 login_required", async () => {
    setBaseUrl(BASE);
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(json({ error: "sign-in required", code: "login_required" }, 401))),
    );
    await expect(portalApi.projects()).rejects.toBeInstanceOf(ApiError);
    expect(hard).toHaveBeenCalledTimes(1);
    const url = new URL(hard.mock.calls[0][0]);
    expect(`${url.origin}${url.pathname}`).toBe(`${BASE}/signin`);
    expect(url.searchParams.get("next")).toBe(window.location.href);
  });

  it("does not redirect on bad_credentials (also 401)", async () => {
    setBaseUrl(BASE);
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(json({ error: "bad", code: "bad_credentials" }, 401))),
    );
    await expect(portalApi.projects()).rejects.toBeInstanceOf(ApiError);
    expect(hard).not.toHaveBeenCalled();
  });

  it("does not redirect without a known base URL (local mode)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(json({ error: "x", code: "login_required" }, 401))),
    );
    await expect(portalApi.projects()).rejects.toBeInstanceOf(ApiError);
    expect(hard).not.toHaveBeenCalled();
  });

  it("never redirects from the base host's own /signin", async () => {
    setBaseUrl(window.location.origin);
    window.history.replaceState(null, "", "/signin");
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(json({ error: "x", code: "login_required" }, 401))),
    );
    await expect(portalApi.projects()).rejects.toBeInstanceOf(ApiError);
    expect(hard).not.toHaveBeenCalled();
  });
});

// ---- pages -------------------------------------------------------------------------

describe("sign-in page", () => {
  /** Open the "Administrator sign-in" disclosure. */
  async function openAdmin(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Administrator sign-in" }));
  }

  it("leads with GitHub: start with next, then go to the authorize URL", async () => {
    fake.state = baseState({ user: null });
    const authorize = "https://github.com/login/oauth/authorize?client_id=x&state=s";
    fake.routes["/api/auth/github/start"] = () => json({ authorize_url: authorize });
    const next = `${ORG}/p/alpha`;
    mount(`/signin?next=${encodeURIComponent(next)}`);
    expect(await screen.findByText("New here? Signing in with GitHub creates your account.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /create one/i })).toBeNull();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Sign in with GitHub" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(authorize));
    const start = fake.calls.find((c) => c.path === "/api/auth/github/start");
    expect(start?.method).toBe("POST");
    expect(start?.body).toEqual({ next });
  });

  it("shows a refused start (throttled) and does not navigate", async () => {
    fake.state = baseState({ user: null });
    fake.routes["/api/auth/github/start"] = () => json({ error: "slow down", code: "throttled" }, 429);
    mount("/signin");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sign in with GitHub" }));
    await screen.findByText(/Too many attempts/);
    expect(hard).not.toHaveBeenCalled();
  });

  it("keeps the password form behind the Administrator sign-in disclosure", async () => {
    fake.state = baseState({ user: null });
    mount("/signin");
    const toggle = await screen.findByRole("button", { name: "Administrator sign-in" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Email")).toBeNull();
    expect(screen.queryByLabelText("Password")).toBeNull();
    const user = userEvent.setup();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByText(/Ask an instance administrator for a reset link/)).toBeInTheDocument();
  });

  it("posts the credentials with next, then follows the server's redirect", async () => {
    fake.state = baseState({ user: null });
    fake.login = { status: 200, body: { redirect: `${ORG}/` } };
    const next = `${ORG}/p/alpha`;
    mount(`/signin?next=${encodeURIComponent(next)}`);
    const user = userEvent.setup();
    await openAdmin(user);
    await user.type(await screen.findByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${ORG}/`));
    const login = fake.calls.find((c) => c.path === "/api/auth/login");
    expect(login?.body).toEqual({ email: "ada@example.com", password: "correct horse battery", next });
  });

  it("shows bad credentials inline and does not navigate", async () => {
    fake.state = baseState({ user: null });
    mount("/signin");
    const user = userEvent.setup();
    await openAdmin(user);
    await user.type(await screen.findByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Incorrect email or password.");
    expect(hard).not.toHaveBeenCalled();
  });

  it("says so when a correct password belongs to a disabled account", async () => {
    fake.state = baseState({ user: null });
    fake.login = { status: 403, body: { error: "this account is disabled", code: "account_disabled" } };
    mount("/signin");
    const user = userEvent.setup();
    await openAdmin(user);
    await user.type(await screen.findByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText(/This account is disabled/);
    expect(hard).not.toHaveBeenCalled();
  });
});

// ---- GitHub's return address ------------------------------------------------------

describe("GitHub callback page", () => {
  function callbackReply(status: number, body: unknown) {
    fake.routes["/api/auth/github/callback"] = () => json(body, status);
  }
  /** Mount the page with the query in both the router and the address bar. */
  function visit(query: string) {
    fake.state = baseState({ user: null });
    window.history.replaceState(null, "", `/auth/github${query}`);
    return mount(`/auth/github${query}`);
  }

  it("strips the query, posts code and state, then follows the redirect", async () => {
    callbackReply(200, { redirect: `${ORG}/p/alpha` });
    visit("?code=the-code&state=the-state");
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${ORG}/p/alpha`));
    expect(window.location.search).toBe("");
    expect(window.location.pathname).toBe("/auth/github");
    const posts = fake.calls.filter((c) => c.path === "/api/auth/github/callback");
    expect(posts).toHaveLength(1);
    expect(posts[0].method).toBe("POST");
    expect(posts[0].body).toEqual({ code: "the-code", state: "the-state" });
  });

  it("says the sign-in was cancelled on GitHub's access_denied (no POST)", async () => {
    visit("?error=access_denied&error_description=x&state=s");
    const alert = await screen.findByTestId("github-callback-error");
    expect(alert).toHaveTextContent("Sign-in was cancelled.");
    expect(window.location.search).toBe("");
    expect(fake.calls.some((c) => c.path === "/api/auth/github/callback")).toBe(false);
    expect(screen.getByRole("link", { name: "Back to sign in" })).toHaveAttribute("href", "/signin");
  });

  it("links to GitHub's security settings when 2FA is required", async () => {
    callbackReply(403, {
      error: "turn on 2FA",
      code: "github_2fa_required",
      fix_url: "https://github.com/settings/security",
    });
    visit("?code=c&state=s");
    const alert = await screen.findByTestId("github-callback-error");
    expect(alert).toHaveTextContent(/two-factor authentication/);
    expect(within(alert).getByRole("link", { name: /two-factor/ })).toHaveAttribute(
      "href",
      "https://github.com/settings/security",
    );
    expect(hard).not.toHaveBeenCalled();
  });

  it.each([
    ["account_disabled", 403, /This account is disabled/],
    ["oauth_state", 400, /The sign-in expired - try again\./],
    ["github_unavailable", 502, /GitHub could not be reached/],
    ["github_auth_failed", 400, /GitHub did not accept the sign-in/],
  ])("renders %s", async (code, status, text) => {
    callbackReply(status, { error: "server words", code });
    visit("?code=c&state=s");
    expect(await screen.findByTestId("github-callback-error")).toHaveTextContent(text);
    expect(hard).not.toHaveBeenCalled();
  });

  it("an incomplete return address posts nothing", async () => {
    visit("?state=s");
    await screen.findByTestId("github-callback-error");
    expect(fake.calls.some((c) => c.path === "/api/auth/github/callback")).toBe(false);
  });
});

describe("org picker", () => {
  const acme = { slug: "acme", name: "Acme", role: "owner", url: ORG };
  const beta = { slug: "beta", name: "Beta", role: "member", url: "http://beta.whygraph.localhost:8765" };

  it("0 orgs says so and offers Create organization, without redirecting (BUG-19)", async () => {
    fake.state = baseState({ user: { ...ada, github_login: "ada" } });
    const router = mount("/orgs");
    await screen.findByRole("heading", { name: "You're not in an organization yet" });
    expect(where(router)).toBe("/orgs");
    expect(screen.getByRole("link", { name: "Create organization" })).toHaveAttribute("href", "/orgs/new");
    expect(screen.getByTestId("join-hint")).toHaveTextContent("Joining a team? Ask an owner to add your GitHub username @ada.");
    expect(screen.queryByRole("link", { name: /Back to your organizations/ })).toBeNull();
    expect(hard).not.toHaveBeenCalled();
  });

  it("1 org with ?stay=1 shows the list (the switcher's All organizations)", async () => {
    fake.orgs = [{ ...acme, new: true }];
    mount("/orgs?stay=1");
    const list = await screen.findByTestId("org-list");
    expect(list).toHaveTextContent("Acme");
    expect(within(list).getByTestId("org-new")).toHaveTextContent("New");
    expect(within(list).getByText("Owner")).toBeInTheDocument();
    expect(hard).not.toHaveBeenCalled();
  });

  it("1 org hands off to it when there is no next", async () => {
    fake.orgs = [acme];
    mount("/orgs");
    await waitFor(() => expect(hard).toHaveBeenCalledWith(ORG));
  });

  it("1 org with a next shows the list instead (no loop)", async () => {
    fake.orgs = [acme];
    mount(`/orgs?next=${encodeURIComponent(`${ORG}/`)}`);
    await screen.findByTestId("org-list");
    expect(hard).not.toHaveBeenCalled();
  });

  it("many orgs list them all, with a Create organization link", async () => {
    fake.orgs = [acme, beta];
    mount("/orgs");
    const list = await screen.findByTestId("org-list");
    expect(list.querySelectorAll("a")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Create organization" })).toBeInTheDocument();
    expect(hard).not.toHaveBeenCalled();
  });
});

describe("reset page", () => {
  it("reads the token from the fragment, removes it from the address bar and posts it", async () => {
    fake.state = baseState({ user: null });
    window.history.replaceState(null, "", "/reset#token=secret-token");
    mount("/reset");
    const pw = await screen.findAllByLabelText(/password/i);
    await waitFor(() => expect(window.location.hash).toBe(""));
    const user = userEvent.setup();
    await user.type(pw[0], "a long enough passphrase");
    if (pw[1]) await user.type(pw[1], "a long enough passphrase");
    await user.click(screen.getByRole("button", { name: /reset|set|save|change/i }));
    await waitFor(() => expect(fake.calls.some((c) => c.path === "/api/auth/reset")).toBe(true));
    expect(fake.calls.find((c) => c.path === "/api/auth/reset")?.body).toEqual({
      token: "secret-token",
      password: "a long enough passphrase",
    });
  });
});

// ---- reader --------------------------------------------------------------------------

describe("reader role (instance admin in a foreign org)", () => {
  it("shows the banner and hides scan, Chat and Add controls", async () => {
    fake.state = orgState("reader", { user: adminAda });
    mount("/p/alpha");
    await screen.findByTestId("reader-banner");
    expect(screen.getByText("Viewing as instance admin (read-only)")).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Project alpha" });
    expect(screen.queryByRole("button", { name: "Rescan" })).toBeNull();
    expect(screen.queryByTestId("chats-section")).toBeNull();
    expect(fake.calls.some((c) => c.path.includes("/chat/sessions"))).toBe(false);
  });

  it("a member sees one Rescan button and the Chats section", async () => {
    fake.state = orgState("member");
    mount("/p/alpha");
    await screen.findByRole("button", { name: "Rescan" });
    const chats = await screen.findByTestId("chats-section");
    expect(within(chats).getByRole("button", { name: "New chat" })).toBeEnabled();
    await within(chats).findByText("No chats yet");
    expect(screen.queryByTestId("reader-banner")).toBeNull();
  });

  it("the Chat route tells the reader why there is no chat (no role to ask for)", async () => {
    fake.state = orgState("reader", { user: adminAda });
    mount("/p/alpha/chat");
    const notice = await screen.findByTestId("chat-read-only");
    expect(notice).toHaveTextContent(
      "You're viewing this organization as an instance administrator. Chat is read-only access, so it is not available.",
    );
    expect(notice).not.toHaveTextContent("Contributor");
  });

  it("hides the scan buttons on the Scans page", async () => {
    fake.state = orgState("reader", { user: adminAda });
    mount("/p/alpha/scans");
    await screen.findByRole("heading", { name: "Scans" });
    expect(screen.queryByRole("button", { name: "Rescan" })).toBeNull();
  });

  function rationale(role: string) {
    fake.state = orgState(role, role === "reader" ? { user: adminAda } : {});
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <ProjectProvider slug="alpha">
          <RationaleTab qualifiedName="a.b" />
        </ProjectProvider>
      </QueryClientProvider>,
    );
  }

  it("hides RationaleTab's Generate button", async () => {
    rationale("reader");
    await screen.findByText(/Viewers can read cards but not generate them/);
    expect(screen.queryByRole("button", { name: "Generate rationale" })).toBeNull();
  });

  it("shows RationaleTab's Generate button to a member", async () => {
    rationale("member");
    await screen.findByRole("button", { name: "Generate rationale" });
  });
});

// ---- project roles (M2f-1) -----------------------------------------------------------

describe("project roles", () => {
  const asViewer = () => {
    fake.state = orgState("member");
    const viewer = { ...project("alpha"), my_role: "viewer", permissions: PERMISSIONS.viewer };
    fake.routes["/api/projects/alpha"] = () => json(viewer);
    fake.routes["/api/projects"] = () => json({ projects: [viewer] });
  };

  it("a contributor has one Rescan button, no Rename and no Save settings", async () => {
    fake.state = orgState("member");
    fake.routes["/api/projects/alpha/config"] = () =>
      json({ config: {}, secrets: { llm: {}, github_token: { set: false, hint: null } }, import: null });
    fake.routes["/api/portal/defaults"] = () =>
      json({ config: {}, secrets: { llm: {}, github_token: { set: false, hint: null } }, no_provider_key: false });
    const router = mount("/p/alpha");
    const rescan = await screen.findByRole("button", { name: "Rescan" });
    await userEvent.setup().click(rescan);
    // No menu: the click is the quick rescan.
    expect(screen.queryByRole("menuitem", { name: "Full rescan" })).toBeNull();
    await waitFor(() =>
      expect(fake.calls.find((c) => c.method === "POST" && c.path === "/api/projects/alpha/scans")?.body).toEqual({
        trigger: "manual",
        analyze: false,
      }),
    );
    void router;
  });

  it("a contributor sees no Rename, Save settings, Full rescan or Danger zone", async () => {
    fake.state = orgState("member");
    fake.routes["/api/projects/alpha/config"] = () =>
      json({ config: {}, secrets: { llm: {}, github_token: { set: false, hint: null } }, import: null });
    fake.routes["/api/portal/defaults"] = () =>
      json({ config: {}, secrets: { llm: {}, github_token: { set: false, hint: null } }, no_provider_key: false });
    mount("/p/alpha/settings");
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.queryByRole("button", { name: "Rename" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save settings" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove project" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Danger zone" })).toBeNull();
  });

  it("a project admin gets the Rescan menu with both choices", async () => {
    fake.state = orgState("admin");
    mount("/p/alpha");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Rescan" }));
    await screen.findByRole("menuitem", { name: "Quick rescan" });
    expect(screen.getByRole("menuitem", { name: "Full rescan" })).toBeInTheDocument();
  });

  it("a viewer has no Rescan, no Chats section, and never requests the sessions", async () => {
    asViewer();
    mount("/p/alpha");
    await screen.findByRole("heading", { name: "Project alpha" });
    expect(screen.queryByRole("button", { name: "Rescan" })).toBeNull();
    expect(screen.queryByTestId("chats-section")).toBeNull();
    expect(fake.calls.some((c) => c.path.includes("/chat/sessions"))).toBe(false);
  });

  it("a viewer's Chat route shows the notice and requests no sessions", async () => {
    asViewer();
    mount("/p/alpha/chat");
    const notice = await screen.findByTestId("chat-read-only");
    expect(notice).toHaveTextContent("Chat needs the Contributor role. Ask a project admin to change your role.");
    expect(fake.calls.some((c) => c.path.includes("/chat/sessions"))).toBe(false);
  });

  it("a viewer's command palette has no New chat entry", async () => {
    asViewer();
    useUi.setState({ paletteOpen: true });
    mount("/p/alpha");
    await screen.findByText("Project settings");
    await waitFor(() => expect(fake.calls.some((c) => c.path === "/api/projects")).toBe(true));
    expect(screen.getByRole("option", { name: "Explorer" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "New chat" })).toBeNull();
  });

  it("a viewer sees the Restricted badge only on a restricted project", async () => {
    fake.state = orgState("member");
    fake.routes["/api/projects/alpha"] = () => json({ ...project("alpha"), restricted: true });
    mount("/p/alpha");
    await screen.findByTestId("restricted-badge");
  });

  it("can() denies on a project without permissions", () => {
    expect(can(undefined, "project.read")).toBe(false);
    expect(can({}, "project.read")).toBe(false);
    expect(can({ permissions: ["project.read"] }, "project.chat")).toBe(false);
    expect(can({ permissions: ["project.chat"] }, "project.chat")).toBe(true);
  });
});

// ---- helpers -------------------------------------------------------------------------

describe("identity helpers", () => {
  it("isSafeNext keeps the base scheme/port and the base host or a subdomain", () => {
    expect(isSafeNext(`${ORG}/p/a`, BASE)).toBe(true);
    expect(isSafeNext(`${BASE}/orgs`, BASE)).toBe(true);
    expect(isSafeNext("http://evil.example:8765/", BASE)).toBe(false);
    expect(isSafeNext("https://acme.whygraph.localhost:8765/", BASE)).toBe(false);
    expect(isSafeNext("http://acme.whygraph.localhost:9999/", BASE)).toBe(false);
    expect(isSafeNext("http://xwhygraph.localhost:8765/", BASE)).toBe(false);
    expect(isSafeNext("not a url", BASE)).toBe(false);
    expect(isSafeNext(undefined, BASE)).toBe(false);
  });

  it("signInUrl encodes next", () => {
    expect(signInUrl(`${BASE}/`, `${ORG}/p/a?x=1`)).toBe(
      `${BASE}/signin?next=${encodeURIComponent(`${ORG}/p/a?x=1`)}`,
    );
  });

  it("authMessage maps the new codes", () => {
    for (const code of [
      "oauth_state",
      "github_auth_failed",
      "github_unavailable",
      "github_2fa_required",
      "account_disabled",
      "no_password",
      "bad_login",
      "already_member",
      "user_disabled",
      "last_owner",
      "not_member",
      "self_disable",
    ]) {
      expect(authMessage(new ApiError(400, "raw server words", code))).not.toBe("raw server words");
    }
  });

  it("authMessage maps the password codes inline", () => {
    expect(authMessage(new ApiError(422, "x", "weak_password"))).toMatch(/15 characters/);
    expect(authMessage(new ApiError(422, "x", "common_password"))).toMatch(/too common/);
    expect(authMessage(new ApiError(429, "slow"))).toMatch(/Too many/);
  });
});

// ---- members (M2d-1) -----------------------------------------------------------------

describe("members page", () => {
  type Row = {
    uid: string;
    display_name: string;
    github_login: string | null;
    avatar_url: string | null;
    role: string;
    joined_at: string;
    disabled: boolean;
  };
  const row = (uid: string, name: string, login: string, role: string): Row => ({
    uid,
    display_name: name,
    github_login: login,
    avatar_url: null,
    role,
    joined_at: "2026-10-01T00:00:00Z",
    disabled: false,
  });
  // Ada (u1, the viewer in `orgState`) plus an owner, an admin and a member.
  let members: Row[];

  function serveMembers(add: { status: number; body: unknown } = { status: 201, body: {} }) {
    fake.routes["/api/org/members"] = (method, body) => {
      if (method === "POST") {
        if (add.status !== 201) return json(add.body, add.status);
        const b = body as { github_login: string; role: string };
        const created = row("u9", "Dan", b.github_login, b.role);
        members.push(created);
        return json(created, 201);
      }
      return json(members);
    };
  }
  function visit(role: string, tweak: (rows: Row[]) => Row[] = (rows) => rows) {
    members = [
      row("u1", "Ada", "ada", role === "reader" ? "member" : role),
      row("u2", "Olga", "olga", "owner"),
      row("u3", "Abe", "abe", "admin"),
      row("u4", "Meg", "meg", "member"),
    ];
    if (role === "reader") members = members.filter((m) => m.uid !== "u1");
    members = tweak(members);
    fake.state = orgState(role, role === "reader" ? { user: adminAda } : {});
    serveMembers();
    return mount("/members");
  }
  const rowOf = async (uid: string) => within(await screen.findByTestId(`member-${uid}`));
  const roleOptions = (select: HTMLElement) =>
    Array.from((select as HTMLSelectElement).options).map((o) => o.value);

  it("an owner adds, re-roles and removes on every row (owner role included)", async () => {
    visit("owner");
    await screen.findByTestId("add-member");
    const addRole = screen.getByLabelText("Role");
    expect(roleOptions(addRole)).toEqual(["member", "admin", "owner"]);

    const olga = await rowOf("u2");
    expect(roleOptions(olga.getByRole("combobox", { name: "Role for Olga" }))).toEqual(["member", "admin", "owner"]);
    expect(olga.getByRole("button", { name: "Remove" })).toBeInTheDocument();
    for (const uid of ["u3", "u4"]) {
      const r = await rowOf(uid);
      expect(r.getByRole("combobox")).toBeInTheDocument();
      expect(r.getByRole("button", { name: "Remove" })).toBeInTheDocument();
    }
    // Ada is not the last owner (Olga is one too), so she may leave.
    expect(screen.getByRole("button", { name: "Leave organization" })).toBeInTheDocument();
  });

  it("an admin adds and acts on members and admins only (no owner option, no owner rows)", async () => {
    visit("admin");
    await screen.findByTestId("add-member");
    expect(roleOptions(screen.getByLabelText("Role"))).toEqual(["member", "admin"]);

    const olga = await rowOf("u2");
    expect(olga.queryByRole("combobox")).toBeNull();
    expect(olga.queryByRole("button", { name: "Remove" })).toBeNull();
    expect(olga.getByText("owner")).toBeInTheDocument();

    const meg = await rowOf("u4");
    expect(roleOptions(meg.getByRole("combobox", { name: "Role for Meg" }))).toEqual(["member", "admin"]);
    expect(meg.getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("a member sees the list and Leave only", async () => {
    visit("member");
    await screen.findByTestId("member-list");
    expect(screen.queryByTestId("add-member")).toBeNull();
    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
    expect(screen.queryAllByRole("button", { name: "Remove" })).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Leave organization" })).toBeInTheDocument();
  });

  it("a reader sees the list only", async () => {
    visit("reader");
    const list = await screen.findByTestId("member-list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.queryByTestId("add-member")).toBeNull();
    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
    expect(screen.queryAllByRole("button", { name: "Remove" })).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Leave organization" })).toBeNull();
  });

  it("hides Leave from the last owner", async () => {
    visit("owner", (rows) => rows.map((m) => (m.uid === "u2" ? { ...m, role: "admin" } : m)));
    await screen.findByTestId("member-u2");
    expect(screen.queryByRole("button", { name: "Leave organization" })).toBeNull();
  });

  it("counts the members in its heading", async () => {
    visit("owner");
    await waitFor(() => expect(screen.getByTestId("members-heading")).toHaveTextContent("Members (4)"));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Members");
    // On a phone the identity takes its own line; details and actions wrap under it.
    for (const identity of screen.getAllByTestId("member-identity")) {
      expect(identity.className).toContain("basis-full");
      expect(identity.parentElement?.className).toContain("row-wrap");
    }
  });

  it("shows project access chips from grants, and none when the server sends no grants", async () => {
    visit("admin", (rows) =>
      rows.map((r) =>
        r.uid === "u4" ? { ...r, grants: [{ project: "api", name: "api", role: "contributor" }] } : r,
      ),
    );
    const meg = await rowOf("u4");
    expect(meg.getByTestId("member-grants-u4")).toHaveTextContent("api: Contributor");
    expect(screen.queryByTestId("member-grants-u3")).toBeNull();
  });

  it("a direct add says what the person will see and links to the row, until dismissed", async () => {
    visit("owner");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("GitHub username"), "dan");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    const note = await screen.findByTestId("member-added");
    expect(note).toHaveTextContent("@dan is now a member. They'll see a welcome note the next time they open Acme.");
    expect(within(note).getByRole("link", { name: "Show in the list" })).toHaveAttribute("href", "#member-u9");
    expect(screen.queryByText("Added @dan")).toBeNull();
    await user.click(within(note).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("member-added")).toBeNull();
  });

  it("adds by GitHub username and role", async () => {
    visit("admin");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("GitHub username"), "@dan");
    await user.selectOptions(screen.getByLabelText("Role"), "admin");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    await screen.findByTestId("member-u9");
    const post = fake.calls.find((c) => c.path === "/api/org/members" && c.method === "POST");
    expect(post?.body).toEqual({ github_login: "@dan", role: "admin" });
  });

  it("words no_such_github_user from the registry", async () => {
    visit("owner");
    serveMembers({ status: 404, body: { error: "GitHub has no user with that username.", code: "no_such_github_user" } });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("GitHub username"), "nobody");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    expect(await screen.findByTestId("add-member-error")).toHaveTextContent("GitHub has no user with that username.");
  });

  it("changes a role with PATCH and removes with DELETE after a confirm", async () => {
    visit("owner");
    fake.routes["/api/org/members/u4"] = (method, body) =>
      method === "DELETE"
        ? new Response(null, { status: 204 })
        : json({ ...members[3], role: (body as { role: string }).role });
    const user = userEvent.setup();
    const meg = await rowOf("u4");
    await user.selectOptions(meg.getByRole("combobox", { name: "Role for Meg" }), "admin");
    await waitFor(() =>
      expect(fake.calls.find((c) => c.path === "/api/org/members/u4" && c.method === "PATCH")?.body).toEqual({
        role: "admin",
      }),
    );
    await user.click(meg.getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(fake.calls.some((c) => c.path === "/api/org/members/u4" && c.method === "DELETE")).toBe(true),
    );
  });

  it("leaving goes to the base host's picker", async () => {
    visit("member");
    fake.routes["/api/org/membership"] = () => new Response(null, { status: 204 });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Leave organization" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Leave organization" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${BASE}/orgs`));
    expect(fake.calls.find((c) => c.path === "/api/org/membership")?.method).toBe("DELETE");
  });

  it("the sidebar has a Members item in production, for every role", async () => {
    for (const role of ["member", "reader"]) {
      fake.state = orgState(role, role === "reader" ? { user: adminAda } : {});
      mount("/");
      const nav = await screen.findByRole("navigation", { name: "Main" });
      expect(await within(nav).findByRole("link", { name: "Members" })).toHaveAttribute("href", "/members");
      document.body.innerHTML = "";
    }
  });
});

describe("local mode has no members", () => {
  const local = { mode: "local", setup_complete: true, user: { uid: "u", display_name: "T", role: "owner" } };

  it("shows no Members item in the sidebar", async () => {
    fake.state = local;
    mount("/");
    await screen.findByText("Project alpha");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Settings" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Members" })).toBeNull();
  });

  it("/members is not a page", async () => {
    fake.state = local;
    mount("/members");
    await screen.findByRole("heading", { name: "Only on a team portal" });
    expect(screen.queryByTestId("member-list")).toBeNull();
    expect(fake.calls.some((c) => c.path === "/api/org/members")).toBe(false);
  });
});

// ---- account, admin and org settings (M2d-1) ------------------------------------------

describe("account page", () => {
  const account = (over: Record<string, unknown>) => ({
    uid: "u1",
    email: null,
    display_name: "Ada",
    is_instance_admin: false,
    github_login: "ada",
    avatar_url: "https://avatars.example/ada.png",
    has_password: false,
    ...over,
  });

  it("shows the GitHub login and avatar, and no password section for a GitHub account", async () => {
    fake.routes["/api/account"] = () => json(account({}));
    mount("/account");
    expect(await screen.findByTestId("account-identity")).toHaveTextContent("@ada");
    expect(document.querySelector('img[src="https://avatars.example/ada.png"]')).not.toBeNull();
    expect(screen.queryByRole("heading", { name: "Change password" })).toBeNull();
    expect(screen.queryByLabelText("Current password")).toBeNull();
  });

  it("lists your organizations with roles and links", async () => {
    fake.routes["/api/account"] = () => json(account({}));
    fake.orgs = [
      { slug: "acme", name: "Acme", role: "owner", url: ORG },
      { slug: "rocket", name: "Rocket", role: "member", url: "http://rocket.whygraph.localhost:8765" },
    ];
    mount("/account");
    const acme = within(await screen.findByTestId("account-org-acme"));
    expect(acme.getByRole("link", { name: /Acme/ })).toHaveAttribute("href", `${ORG}/`);
    expect(acme.getByText("Owner")).toBeInTheDocument();
    expect(within(screen.getByTestId("account-org-rocket")).getByText("Member")).toBeInTheDocument();
  });

  it("offers Connect your agent when no local portal is connected, and not otherwise", async () => {
    fake.routes["/api/account"] = () => json(account({}));
    fake.routes["/api/connect/tokens"] = () => json([]);
    mount("/account");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Connect your agent" }));
    expect(await screen.findByTestId("connect-agent-dialog")).toBeInTheDocument();
    // The base host has no projects to pick from.
    expect(fake.calls.some((c) => c.path === "/api/projects")).toBe(false);
  });

  it("says what still works under a hard stop, with the reset date", async () => {
    fake.routes["/api/account"] = () => json(account({}));
    fake.routes["/api/connect/tokens"] = () => json([]);
    fake.routes["/api/account/usage"] = () =>
      json({
        month: "2026-10",
        resets_at: "2026-11-01T00:00:00Z",
        orgs: [
          { slug: "acme", name: "Acme", url: ORG, spent_usd: 10, calls: 4, budget_usd: 10, pct: 100, hard_stop: true },
          { slug: "rocket", name: "Rocket", url: ORG, spent_usd: 2, calls: 1, budget_usd: 10, pct: 20, hard_stop: true },
        ],
      });
    mount("/account");
    const stopped = await screen.findByTestId("account-usage-stopped-acme");
    expect(stopped).toHaveTextContent("Stopped");
    expect(stopped).toHaveTextContent("You can still read everything that's already generated.");
    // The viewer-locale shape the Usage pages use (CN-3).
    expect(stopped).toHaveTextContent(formatResetsAt("2026-11-01T00:00:00Z"));
    expect(screen.queryByTestId("account-usage-stopped-rocket")).toBeNull();
  });

  it("keeps the password section for a password account", async () => {
    fake.routes["/api/account"] = () =>
      json(account({ email: "ada@example.com", github_login: null, avatar_url: null, has_password: true }));
    mount("/account");
    expect(await screen.findByTestId("account-identity")).toHaveTextContent("ada@example.com");
    expect(await screen.findByRole("heading", { name: "Change password" })).toBeInTheDocument();
  });
});

describe("admin page", () => {
  const user = (over: Record<string, unknown>) => ({
    uid: "u2",
    email: null,
    display_name: "Ben",
    is_instance_admin: false,
    github_login: "ben",
    has_password: false,
    disabled: false,
    created_at: "2026-10-01T00:00:00Z",
    org_count: 1,
    ...over,
  });
  let users: ReturnType<typeof user>[];

  beforeEach(() => {
    fake.state = baseState({ user: adminAda });
    users = [
      user({ uid: "u1", email: "ada@example.com", display_name: "Ada", github_login: null, has_password: true, is_instance_admin: true }),
      user({}),
    ];
    fake.routes["/api/admin/settings"] = () => json({ base_url: BASE, base_check: [] });
    fake.routes["/api/admin/orgs"] = () => json([]);
    fake.routes["/api/admin/users"] = () => json(users);
    fake.routes["/api/admin/users/u2"] = (_method, body) => {
      users = users.map((u) => (u.uid === "u2" ? { ...u, ...(body as object) } : u));
      return json(users[1]);
    };
  });

  it("lists @login or email by uid, reset links only for password accounts", async () => {
    mount("/admin");
    const ben = within(await screen.findByTestId("admin-user-u2"));
    expect(ben.getByText("@ben")).toBeInTheDocument();
    expect(ben.queryByRole("button", { name: "Copy reset link" })).toBeNull();
    const ada = within(screen.getByTestId("admin-user-u1"));
    expect(ada.getByText("ada@example.com")).toBeInTheDocument();
    expect(ada.getByRole("button", { name: "Copy reset link" })).toBeInTheDocument();
    // Your own account cannot be disabled from here.
    expect(ada.queryByRole("button", { name: "Disable" })).toBeNull();
  });

  it("shows the settings check card, clean or with problems", async () => {
    mount("/admin");
    expect(await screen.findByTestId("base-check-ok")).toHaveTextContent("found no problems");
  });

  it("lists the base URL problems in the settings check card", async () => {
    fake.routes["/api/admin/settings"] = () => json({ base_url: BASE, base_check: ["*.whygraph.localhost does not resolve"] });
    mount("/admin");
    expect(await screen.findByTestId("base-check")).toHaveTextContent("does not resolve");
  });

  it("makes an admin with a secondary button", async () => {
    mount("/admin");
    const ben = within(await screen.findByTestId("admin-user-u2"));
    expect(ben.getByRole("button", { name: "Make admin" }).className).toContain("bg-secondary");
  });

  it("disables only after a confirmation, and enables at once", async () => {
    mount("/admin");
    const u = userEvent.setup();
    const ben = within(await screen.findByTestId("admin-user-u2"));
    await u.click(ben.getByRole("button", { name: "Disable" }));
    expect(fake.calls.some((c) => c.path === "/api/admin/users/u2" && c.method === "PATCH")).toBe(false);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Disable Ben?");
    await u.click(within(dialog).getByRole("button", { name: "Disable" }));
    await waitFor(() =>
      expect(fake.calls.find((c) => c.path === "/api/admin/users/u2")?.body).toEqual({ disabled: true }),
    );
    expect(await ben.findByText("disabled")).toBeInTheDocument();
    await u.click(ben.getByRole("button", { name: "Enable" }));
    await waitFor(() =>
      expect(fake.calls.filter((c) => c.path === "/api/admin/users/u2").at(-1)?.body).toEqual({ disabled: false }),
    );
  });
});

describe("org settings page", () => {
  const noKey = { set: false, hint: null };
  beforeEach(() => {
    fake.routes["/api/portal/defaults"] = () =>
      json({
        config: {},
        secrets: { llm: { anthropic: noKey, openai: noKey, deepseek: noKey, openrouter: noKey }, github_token: noKey },
        no_provider_key: false,
      });
  });

  it("is editable for an owner", async () => {
    fake.state = orgState("owner");
    mount("/settings");
    await screen.findByTestId("config-form");
    expect(within(screen.getByRole("region", { name: "Models and keys" })).getByRole("button", { name: "Save" })).toBeInTheDocument();
    expect(screen.queryByTestId("settings-owner-only")).toBeNull();
  });

  it("is read-only for an admin (org.configure is owner-only)", async () => {
    fake.state = orgState("admin");
    mount("/settings");
    await screen.findByTestId("config-form");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.getByTestId("settings-owner-only")).toBeInTheDocument();
  });
});

// ---- invitations, org settings, project access, audit (M2f-1) -------------------------

describe("invitations", () => {
  const invitation = (uid: string, login: string, over: Record<string, unknown> = {}) => ({
    uid,
    github_login: login,
    role: "member",
    status: "open",
    invited_by: { uid: "u1", display_name: "Ada", github_login: "ada" },
    created_at: "2026-10-01T00:00:00Z",
    expires_at: "2026-10-15T00:00:00Z",
    redeemed_at: null,
    revoked_at: null,
    grants: [],
    ...over,
  });
  let invitations: ReturnType<typeof invitation>[];

  function visit(role = "admin") {
    invitations = [];
    fake.state = orgState(role);
    fake.routes["/api/org/members"] = (method) => (method === "POST" ? json({}, 500) : json([]));
    fake.routes["/api/org/invitations"] = () => json(invitations);
    return mount("/members");
  }

  it("invites with a role and project grants, then shows the link to share", async () => {
    visit();
    fake.routes["/api/org/members"] = (method, body) => {
      if (method !== "POST") return json([]);
      const b = body as { github_login: string; role: string };
      const created = invitation("i9", b.github_login, { role: b.role });
      invitations.push(created);
      return json({ ...created, pending: true }, 201);
    };
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("GitHub username"), "dan");
    expect(screen.queryByText(/must have signed in/i)).toBeNull();
    expect(screen.getByTestId("role-help")).toHaveTextContent(/default project role/i);
    await user.selectOptions(await screen.findByLabelText("Access to Project alpha"), "viewer");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    const note = await screen.findByTestId("invite-pending");
    expect(note).toHaveAttribute("data-slot", "alert");
    expect(note.className).toContain("bg-info-soft");
    expect(note).toHaveTextContent("No message is sent. Share this link with @dan");
    expect(within(note).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    // The link is a command block that breaks after "/" (never break-all, no
    // reserved right column for Dismiss, which sits in the flow).
    const link = note.querySelector("pre")!;
    expect(link.textContent).toBe(window.location.origin);
    expect(link.className).not.toContain("break-all");
    expect(note.innerHTML).not.toContain("pr-20");
    expect(within(note).getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    const post = fake.calls.find((c) => c.path === "/api/org/members" && c.method === "POST");
    expect(post?.body).toEqual({ github_login: "dan", role: "member", grants: [{ project: "alpha", role: "viewer" }] });
    // The new invitation shows up in the pending list.
    expect(await screen.findByTestId("invitation-i9")).toHaveTextContent("@dan");
  });

  it("keeps the project access select wide enough for Organization default", async () => {
    visit();
    const select = await screen.findByLabelText("Access to Project alpha");
    expect(select.className).not.toContain("w-36");
    expect(select.className).toContain("w-48");
  });

  it("shows the invite form as a skeleton until the projects load (ER-5)", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    fake.routes["/api/projects"] = () =>
      gate.then(() => json({ projects: [project("alpha")] })) as unknown as Response;
    visit();
    expect(await screen.findByTestId("add-member-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("add-member")).toBeNull();
    release();
    const form = await screen.findByTestId("add-member");
    // The access fieldset is there with the form, not popping in after it.
    expect(within(form).getByTestId("invite-grants")).toBeInTheDocument();
  });

  it("says when the invitations fail to load, with Retry, instead of hiding the section (ER-4)", async () => {
    visit();
    let fail = true;
    fake.routes["/api/org/invitations"] = () => (fail ? json({ error: "boom" }, 500) : json([invitation("i1", "dan")]));
    const section = within(await screen.findByTestId("invitations"));
    expect(await section.findByText("Couldn't load the invitations")).toBeInTheDocument();
    fail = false;
    await userEvent.setup().click(section.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("invitation-i1")).toHaveTextContent("@dan");
  });

  it("keeps one notice per invite until dismissed; the next submit does not clear it", async () => {
    visit();
    let n = 0;
    fake.routes["/api/org/members"] = (method, body) => {
      if (method !== "POST") return json([]);
      const b = body as { github_login: string; role: string };
      const created = invitation(`i${++n}`, b.github_login, { role: b.role });
      invitations.push(created);
      return json({ ...created, pending: true }, 201);
    };
    const user = userEvent.setup();
    for (const login of ["dan", "eve"]) {
      await user.type(await screen.findByLabelText("GitHub username"), login);
      await user.click(screen.getByRole("button", { name: "Invite" }));
      await waitFor(() => expect(fake.calls.filter((c) => c.method === "POST")).toHaveLength(login === "dan" ? 1 : 2));
    }
    const notes = await screen.findAllByTestId("invite-pending");
    expect(notes).toHaveLength(2);
    expect(notes[0]).toHaveTextContent("@dan");
    expect(notes[1]).toHaveTextContent("@eve");
    await user.click(within(notes[0]).getByRole("button", { name: "Dismiss" }));
    expect(screen.getAllByTestId("invite-pending")).toHaveLength(1);
    expect(screen.getByTestId("invite-pending")).toHaveTextContent("@eve");
  });

  it("lists open and expired invitations, with closed ones collapsed", async () => {
    visit();
    invitations.push(
      invitation("i1", "open", {}),
      invitation("i2", "late", { status: "expired" }),
      invitation("i3", "done", { status: "redeemed", redeemed_at: "2026-10-03T00:00:00Z" }),
      invitation("i4", "gone", { status: "revoked", revoked_at: "2026-10-03T00:00:00Z" }),
    );
    const user = userEvent.setup();
    const late = within(await screen.findByTestId("invitation-i2"));
    expect(late.getByText(/^Expired/)).toBeInTheDocument();
    expect(late.getByRole("button", { name: "Revoke" })).toBeInTheDocument();
    expect(screen.getByTestId("invitation-i1")).toHaveTextContent(/Open, expires/);
    expect(screen.queryByTestId("invitation-i3")).toBeNull();
    await user.click(screen.getByRole("button", { name: /closed invitations/ }));
    const done = within(await screen.findByTestId("invitation-i3"));
    expect(done.getByText(/^Accepted/)).toBeInTheDocument();
    expect(done.queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(screen.getByTestId("invitation-i4")).toHaveTextContent("Revoked");
  });

  it("an owner and an admin see the grants only for the member role", async () => {
    visit("owner");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Role"), "admin");
    expect(screen.queryByTestId("invite-grants")).toBeNull();
  });

  it("lists pending invitations and revokes one after a confirm", async () => {
    visit();
    invitations.push(invitation("i1", "eve", { grants: [{ project: "alpha", role: "viewer" }] }));
    fake.routes["/api/org/invitations/i1"] = () => new Response(null, { status: 204 });
    const user = userEvent.setup();
    const row = within(await screen.findByTestId("invitation-i1"));
    expect(row.getByText("@eve")).toBeInTheDocument();
    expect(row.getByText("Ada")).toBeInTheDocument();
    expect(row.getByText("alpha: Viewer")).toBeInTheDocument();
    await user.click(row.getByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() =>
      expect(fake.calls.some((c) => c.path === "/api/org/invitations/i1" && c.method === "DELETE")).toBe(true),
    );
  });

  it("only an owner can revoke an owner invitation", async () => {
    visit("admin");
    invitations.push(invitation("i2", "zed", { role: "owner" }));
    const row = within(await screen.findByTestId("invitation-i2"));
    expect(row.queryByRole("button", { name: "Revoke" })).toBeNull();
  });
});

describe("org settings (general and ownership)", () => {
  const owner = () =>
    orgState("owner", {
      org: { slug: "acme", name: "Acme", role: "owner", default_project_role: "contributor" },
    });
  beforeEach(() => {
    fake.state = owner();
    fake.routes["/api/portal/defaults"] = () => json({ config: {}, secrets: { llm: {} }, no_provider_key: false });
    fake.routes["/api/org/members"] = () =>
      json([
        { uid: "u1", display_name: "Ada", github_login: "ada", avatar_url: null, role: "owner", joined_at: "2026-10-01T00:00:00Z", disabled: false },
        { uid: "u2", display_name: "Meg", github_login: "meg", avatar_url: null, role: "member", joined_at: "2026-10-01T00:00:00Z", disabled: false },
      ]);
  });

  it("renames the org and changes the default project role", async () => {
    fake.routes["/api/org"] = () => json({ slug: "acme", name: "Acme Inc", default_project_role: "viewer" });
    mount("/settings");
    const general = within(await screen.findByTestId("org-general"));
    const save = general.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    const user = userEvent.setup();
    const name = general.getByLabelText("Organization name");
    await user.clear(name);
    await user.type(name, "Acme Inc");
    await user.selectOptions(general.getByLabelText("Default project role"), "viewer");
    await user.click(save);
    await waitFor(() => expect(fake.calls.find((c) => c.path === "/api/org" && c.method === "PATCH")).toBeTruthy());
    expect(fake.calls.find((c) => c.path === "/api/org" && c.method === "PATCH")?.body).toEqual({
      name: "Acme Inc",
      default_project_role: "viewer",
    });
  });

  it("transfers ownership only after the slug is typed", async () => {
    fake.routes["/api/org/transfer"] = () => json({ ok: true });
    mount("/settings");
    const ownership = within(await screen.findByTestId("org-ownership"));
    const user = userEvent.setup();
    // Ada is the caller: only Meg is offered.
    const select = ownership.getByLabelText("New owner") as HTMLSelectElement;
    await waitFor(() => expect(Array.from(select.options).map((o) => o.value)).toEqual(["", "u2"]));
    await user.selectOptions(select, "u2");
    await user.click(ownership.getByRole("button", { name: "Transfer ownership" }));
    const dialog = within(await screen.findByTestId("transfer-dialog"));
    const confirm = dialog.getByRole("button", { name: "Transfer ownership" });
    expect(confirm).toBeDisabled();
    await user.type(dialog.getByLabelText(/Type/), "acme");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() =>
      expect(fake.calls.find((c) => c.path === "/api/org/transfer")?.body).toEqual({ user_uid: "u2", confirm_slug: "acme" }),
    );
  });

  it("says when the members fail to load instead of an empty New owner select (ER-4)", async () => {
    let fail = true;
    fake.routes["/api/org/members"] = () =>
      fail
        ? json({ error: "boom" }, 500)
        : json([
            { uid: "u1", display_name: "Ada", github_login: "ada", avatar_url: null, role: "owner", joined_at: "2026-10-01T00:00:00Z", disabled: false },
            { uid: "u2", display_name: "Meg", github_login: "meg", avatar_url: null, role: "member", joined_at: "2026-10-01T00:00:00Z", disabled: false },
          ]);
    mount("/settings");
    const ownership = within(await screen.findByTestId("org-ownership"));
    expect(await ownership.findByText("Couldn't load the members")).toBeInTheDocument();
    expect(ownership.queryByLabelText("New owner")).toBeNull();
    fail = false;
    await userEvent.setup().click(ownership.getByRole("button", { name: "Retry" }));
    const select = (await ownership.findByLabelText("New owner")) as HTMLSelectElement;
    await waitFor(() => expect(select).toBeEnabled());
    // Nothing chosen yet: Transfer is disabled and says why.
    expect(ownership.getByRole("button", { name: "Transfer ownership" })).toBeDisabled();
    expect(ownership.getByText("Choose the new owner first.")).toBeInTheDocument();
  });

  it("disables New owner with a reason when no other member can take it", async () => {
    fake.routes["/api/org/members"] = () =>
      json([{ uid: "u1", display_name: "Ada", github_login: "ada", avatar_url: null, role: "owner", joined_at: "2026-10-01T00:00:00Z", disabled: false }]);
    mount("/settings");
    const ownership = within(await screen.findByTestId("org-ownership"));
    expect(await ownership.findByText(/No other active member can become the owner/)).toBeInTheDocument();
    expect(ownership.getByLabelText("New owner")).toBeDisabled();
  });

  it("an admin sees neither section", async () => {
    fake.state = orgState("admin");
    mount("/settings");
    await screen.findByTestId("config-form");
    expect(screen.queryByTestId("org-general")).toBeNull();
    expect(screen.queryByTestId("org-ownership")).toBeNull();
  });
});

describe("project access section", () => {
  const person = (uid: string, name: string, org_role: string, project_role: string | null, source: string) => ({
    uid,
    login: name.toLowerCase(),
    name,
    avatar: null,
    org_role,
    project_role,
    source,
  });
  beforeEach(() => {
    fake.state = orgState("admin");
    fake.routes["/api/projects/alpha/access"] = (method) =>
      method === "PATCH"
        ? json({ restricted: true })
        : json({
            restricted: false,
            org_default: "viewer",
            people: [
              person("u1", "Ada", "admin", "admin", "org_admin"),
              person("u2", "Meg", "member", "contributor", "grant"),
              person("u3", "Sam", "member", "viewer", "default"),
            ],
            invitations: [],
          });
  });

  it("shows who holds which role and why, and flips Restricted", async () => {
    mount("/p/alpha/settings");
    const section = within(await screen.findByTestId("project-access"));
    expect(await section.findByTestId("access-u1")).toHaveTextContent("as org admin");
    expect(section.getByRole("combobox", { name: "Role for Meg" })).toHaveValue("contributor");
    expect(section.getByTestId("access-default")).toHaveTextContent("Viewer (the organization's default)");
    const user = userEvent.setup();
    await user.click(section.getByRole("switch", { name: /Restricted/ }));
    await waitFor(() =>
      expect(fake.calls.find((c) => c.path === "/api/projects/alpha/access" && c.method === "PATCH")?.body).toEqual({
        restricted: true,
      }),
    );
  });

  it("changes, adds and removes grants", async () => {
    fake.routes["/api/projects/alpha/access/u2"] = (method) =>
      method === "DELETE" ? new Response(null, { status: 204 }) : json({});
    fake.routes["/api/projects/alpha/access/u3"] = () => json({});
    mount("/p/alpha/settings");
    const section = within(await screen.findByTestId("project-access"));
    const user = userEvent.setup();
    await user.selectOptions(await section.findByRole("combobox", { name: "Role for Meg" }), "admin");
    await waitFor(() =>
      expect(fake.calls.find((c) => c.path === "/api/projects/alpha/access/u2" && c.method === "PUT")?.body).toEqual({
        role: "admin",
      }),
    );
    await user.selectOptions(section.getByLabelText("Add person"), "u3");
    await user.click(section.getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(fake.calls.find((c) => c.path === "/api/projects/alpha/access/u3" && c.method === "PUT")?.body).toEqual({
        role: "viewer",
      }),
    );
    await user.click(section.getByTestId("access-u2").querySelector("button") as HTMLElement);
    // Removing someone's access asks first.
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(fake.calls.some((c) => c.path === "/api/projects/alpha/access/u2" && c.method === "DELETE")).toBe(true),
    );
  });

  it("is hidden without project.access", async () => {
    fake.state = orgState("member");
    mount("/p/alpha/settings");
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.queryByTestId("project-access")).toBeNull();
    expect(fake.calls.some((c) => c.path === "/api/projects/alpha/access")).toBe(false);
  });
});

describe("audit log", () => {
  const event = (id: number, name: string) => ({
    id,
    created_at: "2026-10-05T10:00:00Z",
    org: "acme",
    actor: { uid: "u1", label: "@ada" },
    event: name,
    target: null,
    ip: "127.0.0.1",
    fields: { role: "admin" },
  });

  it("filters, pages with Load more and downloads the CSV through a header-carrying fetch", async () => {
    fake.state = orgState("owner");
    fake.routes["/api/org/audit"] = () => {
      const call = fake.calls[fake.calls.length - 1];
      return call.search.includes("before=2") ? json({ events: [event(1, "member_added")], next: null }) : json({ events: [event(3, "org_renamed"), event(2, "invitation_created")], next: 2 });
    };
    fake.routes["/api/org/audit.csv"] = () =>
      new Response("id,event\n3,org_renamed\n", { status: 200, headers: { "content-type": "text/csv" } });
    const created = vi.fn(() => "blob:csv");
    const revoked = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: created, revokeObjectURL: revoked }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    mount("/audit");
    expect(await screen.findByTestId("audit-3")).toHaveTextContent("Organization renamed");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByTestId("audit-1")).toHaveTextContent("Member added");
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();

    await user.selectOptions(screen.getByLabelText("Event"), "org_renamed");
    await user.type(screen.getByLabelText("Actor"), "@ada");
    await user.click(screen.getByRole("button", { name: "Filter" }));
    await waitFor(() =>
      expect(fake.calls.some((c) => c.path === "/api/org/audit" && c.search.includes("event=org_renamed") && c.search.includes("actor=%40ada"))).toBe(true),
    );

    await user.click(screen.getByRole("button", { name: "Download CSV" }));
    await waitFor(() => expect(created).toHaveBeenCalled());
    const csv = fake.calls.find((c) => c.path === "/api/org/audit.csv");
    expect(csv?.headers["X-WhyGraph-Client"]).toBe("1");
    expect(csv?.search).toContain("event=org_renamed");
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });

  it("labels events, names the target, shows details as pairs and calls a null actor System", async () => {
    fake.state = orgState("owner");
    fake.routes["/api/org/audit"] = () =>
      json({
        events: [
          {
            ...event(7, "budget_set"),
            target: "u4",
            target_label: "Meg (@meg)",
            fields: { scope: "member", monthly_usd: "25.00", hard_stop: true },
          },
          { ...event(6, "key_tested"), actor: null, fields: { provider: "openai", result: "ok" } },
        ],
        next: null,
      });
    mount("/audit");
    const budget = within(await screen.findByTestId("audit-7"));
    expect(budget.getByText("Budget set")).toBeInTheDocument();
    expect(budget.getByText("Meg (@meg)")).toBeInTheDocument();
    expect(budget.getByText("Monthly budget").nextSibling).toHaveTextContent("$25");
    expect(budget.getByText("Hard stop").nextSibling).toHaveTextContent("Yes");
    const tested = within(screen.getByTestId("audit-6"));
    expect(tested.getByText("API key tested")).toBeInTheDocument();
    expect(tested.getByText("System")).toBeInTheDocument();
    expect(tested.getByText("Provider").nextSibling).toHaveTextContent("OpenAI");
    expect(tested.getByText("Result").nextSibling).toHaveTextContent("Works");
  });

  it("words codes, shortens ids, formats rates and leaves empty fields out (MEM-1)", async () => {
    fake.state = orgState("owner");
    const inv = "76b37542-8a8a-4c1e-9d2b-0f1e2d3c4b5a";
    fake.routes["/api/org/audit"] = () =>
      json({
        events: [
          {
            ...event(9, "member_add_refused"),
            fields: { reason: "no_such_github_user", github_login: "ghost", role: "member" },
          },
          { ...event(8, "invitation_created"), target: inv, fields: { invitation: inv, scope: "org", grants: 0 } },
          {
            ...event(5, "price_override_set"),
            fields: { provider: "anthropic", model: "claude-x", input_per_mtok: "3.000000", cache_read_per_mtok: null },
          },
          { ...event(4, "github_signin_refused"), fields: { reason: "2fa_required", github_login: "ada" } },
        ],
        next: null,
      });
    mount("/audit");
    const refused = within(await screen.findByTestId("audit-9"));
    expect(refused.getByText("Reason").nextSibling).toHaveTextContent("No such GitHub user");
    expect(refused.getByText("GitHub login").nextSibling).toHaveTextContent("@ghost");
    expect(refused.getByText("Role").nextSibling).toHaveTextContent("Member");
    const created = within(screen.getByTestId("audit-8"));
    // The raw UUID is never printed whole: its first block, muted mono, whole in the title.
    expect(screen.getByTestId("audit-8")).not.toHaveTextContent(inv);
    const id = created.getByText("Invitation").nextSibling as HTMLElement;
    expect(id).toHaveTextContent("76b37542");
    expect(id.querySelector("[title]")).toHaveAttribute("title", inv);
    expect(id.querySelector("[title]")?.className).toContain("font-mono");
    expect(created.getByText("Scope").nextSibling).toHaveTextContent("Organization");
    const price = within(screen.getByTestId("audit-5"));
    expect(price.getByText("Input price").nextSibling).toHaveTextContent("$3.00 per million tokens");
    // A null rate is left out, not printed as a bare label or a dash.
    expect(price.queryByText("Cache read price")).toBeNull();
    const twoFactor = within(screen.getByTestId("audit-4"));
    expect(twoFactor.getByText("Reason").nextSibling).toHaveTextContent("Two-factor authentication is required");
    // Values wrap between segments (after "@", "." ...), never mid-word.
    expect(document.body).not.toHaveTextContent(/no_such_github_user|2fa_required|Github login/);
  });

  it("filters by event in a grouped select", async () => {
    fake.state = orgState("owner");
    fake.routes["/api/org/audit"] = () => json({ events: [], next: null });
    mount("/audit");
    const select = (await screen.findByLabelText("Event")) as HTMLSelectElement;
    const groups = Array.from(select.querySelectorAll("optgroup")).map((g) => g.label);
    expect(groups).toEqual(["Members", "Projects", "Budgets", "Security", "Organization"]);
    expect(within(select).getByRole("option", { name: "API key tested" })).toHaveValue("key_tested");
  });

  it("is a page and a sidebar item for owners only", async () => {
    fake.state = orgState("admin");
    mount("/audit");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).queryByRole("link", { name: "Audit log" })).toBeNull();
    expect(screen.queryByTestId("audit-table")).toBeNull();
  });

  it("owners get the sidebar item", async () => {
    fake.state = orgState("owner");
    fake.routes["/api/org/audit"] = () => json({ events: [], next: null });
    mount("/members");
    fake.routes["/api/org/members"] = () => json([]);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(await within(nav).findByRole("link", { name: "Audit log" })).toHaveAttribute("href", "/audit");
  });
});

describe("admin security events", () => {
  it("lists the orgless events on the admin page", async () => {
    fake.state = baseState({ user: adminAda });
    fake.routes["/api/admin/settings"] = () => json({ base_url: BASE, warnings: [] });
    fake.routes["/api/admin/users"] = () => json([]);
    fake.routes["/api/admin/orgs"] = () => json([]);
    fake.routes["/api/admin/audit"] = () =>
      json({ events: [{ id: 5, created_at: "2026-10-05T10:00:00Z", org: null, actor: null, event: "github_signin", target: null, ip: null, fields: {} }], next: null });
    mount("/admin");
    const box = within(await screen.findByTestId("admin-audit"));
    expect(await box.findByText("Signed in with GitHub")).toBeInTheDocument();
    expect(box.getAllByText("System").length).toBeGreaterThan(0);
    expect(box.queryByRole("button", { name: "Download CSV" })).toBeNull();
  });
});

// ---- the shell in production (S14: NAV-2, NAV-7, NAV-8, ER-7, ER-8, MEM-2, MEM-6) --------

describe("sign-in copy", () => {
  it("names where a valid next lands", async () => {
    fake.state = baseState({ user: null });
    mount(`/signin?next=${encodeURIComponent(`${ORG}/p/alpha`)}`);
    expect(await screen.findByText("Sign in to continue to acme.whygraph.localhost:8765/p/alpha.")).toBeInTheDocument();
  });

  it("says a local portal's connect is waiting", async () => {
    fake.state = baseState({ user: null });
    mount(`/signin?next=${encodeURIComponent(`${BASE}/connect?state=s&client_name=laptop`)}`);
    expect(await screen.findByText("Sign in to connect your local portal.")).toBeInTheDocument();
  });

  it("says the session ended when an org host sent the person back", async () => {
    fake.state = baseState({ user: null });
    mount(`/signin?next=${encodeURIComponent(`${ORG}/p/alpha`)}&reauth=1`);
    expect(await screen.findByText("Your session ended. Sign in to continue.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Session not received" })).toBeNull();
  });

  it("keeps the plain copy without a next or with an unsafe one", async () => {
    fake.state = baseState({ user: null });
    mount(`/signin?next=${encodeURIComponent("https://evil.example/")}`);
    expect(await screen.findByText("Sign in to WhyGraph.")).toBeInTheDocument();
  });
});

describe("base-host chrome", () => {
  const acme = { slug: "acme", name: "Acme", role: "owner", url: ORG };

  it("has the logo, Organizations, an avatar menu and Back to <org> for one of the caller's orgs", async () => {
    fake.orgs = [acme];
    mount("/orgs?stay=1&from=acme");
    const back = await screen.findByTestId("back-to-org");
    expect(back).toHaveTextContent("Back to Acme");
    expect(back).toHaveAttribute("href", ORG);
    const nav = screen.getByRole("navigation", { name: "Account" });
    expect(within(nav).getByRole("link", { name: "Organizations" })).toHaveAttribute("href", "/orgs?stay=1&from=acme");
    expect(within(nav).queryByRole("link", { name: "Administration" })).toBeNull();
    const user = userEvent.setup();
    await user.click(within(nav).getByRole("button", { name: "Account menu" }));
    expect(await screen.findByRole("menuitem", { name: "Account" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
    // The chrome has the logo: the card does not repeat it.
    expect(screen.getAllByText("WhyGraph")).toHaveLength(1);
    expect(document.title).toBe("Organizations · WhyGraph");
  });

  it("ignores a from that is not one of the caller's orgs", async () => {
    fake.orgs = [acme];
    mount("/orgs?stay=1&from=evil");
    await screen.findByTestId("org-list");
    expect(screen.queryByTestId("back-to-org")).toBeNull();
  });

  it("offers Administration to instance admins", async () => {
    fake.state = baseState({ user: adminAda });
    fake.orgs = [acme, { ...acme, slug: "beta", name: "Beta" }];
    mount("/orgs");
    const nav = await screen.findByRole("navigation", { name: "Account" });
    expect(within(nav).getByRole("link", { name: "Administration" })).toHaveAttribute("href", "/admin");
  });

  it("puts the Account page in the same chrome, with the one Sign out in the avatar menu", async () => {
    fake.routes["/api/account"] = () =>
      json({ uid: "u1", email: null, display_name: "Ada", is_instance_admin: false, github_login: "ada", avatar_url: null, has_password: false });
    mount("/account");
    await screen.findByTestId("account-identity");
    expect(screen.getByRole("navigation", { name: "Account" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });
});

describe("org switcher and account menu (org host)", () => {
  const acme = { slug: "acme", name: "Acme", role: "owner", url: ORG };
  const beta = { slug: "beta", name: "Beta", role: "member", url: "http://beta.whygraph.localhost:8765", new: true };

  it("lists who is signed in, the current org, the other orgs with a New dot, and the base links", async () => {
    fake.state = orgState("owner", { version: "2.0.0", user: { ...ada, github_login: "ada" } });
    fake.orgs = [acme, beta];
    mount("/");
    const trigger = await screen.findByTestId("org-switcher");
    expect(trigger).toHaveTextContent("Acme");
    // Fetched on first open only.
    expect(fake.calls.some((c) => c.path === "/api/account/orgs")).toBe(false);
    const user = userEvent.setup();
    await user.click(trigger);
    expect(await screen.findByText("Signed in as ada@example.com")).toBeInTheDocument();
    expect(screen.getByTestId("org-current")).toHaveTextContent("Acme");
    expect(screen.getByTestId("org-current")).toHaveTextContent("Owner");
    const other = await screen.findByTestId("org-beta");
    expect(other).toHaveTextContent("Beta");
    expect(within(other).getByTestId("org-new")).toBeInTheDocument();
    expect(screen.queryByTestId("org-acme")).toBeNull();
    await user.click(screen.getByRole("menuitem", { name: "All organizations" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${BASE}/orgs?stay=1&from=acme`));
  });

  it("goes to another org's host and to Create organization", async () => {
    fake.state = orgState("owner");
    fake.orgs = [acme, beta];
    mount("/");
    const user = userEvent.setup();
    await user.click(await screen.findByTestId("org-switcher"));
    await user.click(await screen.findByTestId("org-beta"));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(beta.url));
    await user.click(await screen.findByTestId("org-switcher"));
    await user.click(await screen.findByRole("menuitem", { name: "Create organization" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${BASE}/orgs/new`));
  });

  it("shows the login and role on the account menu, with the version inside it and no Switch organization", async () => {
    fake.state = orgState("member", { version: "2.0.0", user: { ...ada, github_login: "ada" } });
    mount("/");
    expect(await screen.findByTestId("account-who")).toHaveTextContent("@ada · Member");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Account menu" }));
    expect(await screen.findByText("WhyGraph 2.0.0")).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Switch organization" })).toBeNull();
    await user.click(screen.getByRole("menuitem", { name: "Account" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(`${BASE}/account?from=acme`));
  });

  it("keeps the role whole beside a long email, and the org switcher on its own row with the full name", async () => {
    fake.state = orgState("reader", {
      user: { ...ada, email: "ada.lovelace.with.a.long.address@example.com" },
      org: { slug: "acme", name: "Rocket Laboratories", role: "reader" },
    });
    mount("/");
    const who = await screen.findByTestId("account-who");
    expect(who).toHaveTextContent("ada.lovelace.with.a.long.address@example.com · Read-only");
    const role = within(who).getByText(/Read-only/);
    expect(role.className).toContain("shrink-0");
    expect(role.className).not.toContain("truncate");
    const trigger = await screen.findByTestId("org-switcher");
    expect(trigger).toHaveAttribute("title", "Rocket Laboratories");
    expect(trigger.className).not.toMatch(/max-w-\[/);
    expect(trigger.className).toContain("w-full");
  });

  it("offers Import a repository in the project switcher to owners and admins only", async () => {
    fake.state = orgState("admin");
    mount("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Switch project" }));
    expect(await screen.findByRole("menuitem", { name: "Import a repository" })).toBeInTheDocument();
    document.body.innerHTML = "";
    fake.state = orgState("member");
    mount("/");
    await user.click(await screen.findByRole("button", { name: "Switch project" }));
    await screen.findByRole("menuitem", { name: /Project alpha/ });
    expect(screen.queryByRole("menuitem", { name: "Import a repository" })).toBeNull();
  });

  it("tells a non-owner who can open the audit log (ER-8)", async () => {
    fake.state = orgState("admin");
    mount("/audit");
    expect(await screen.findByText("You don't have access to the audit log. An organization owner can give you access.")).toBeInTheDocument();
    expect(fake.calls.some((c) => c.path === "/api/org/audit")).toBe(false);
  });
});
