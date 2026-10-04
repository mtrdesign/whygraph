import { render, screen, waitFor, within } from "@testing-library/react";
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

// `hardNavigate` is mocked globally (src/test/setup.ts); redirect tests wait for
// the spy rather than for the router, because a hard navigation never resolves.
const hard = vi.mocked(hardNavigate);

const BASE = "http://whygraph.localhost:8765";
const ORG = "http://acme.whygraph.localhost:8765";

type Handler = (method: string, body: unknown) => Response;

interface Fake {
  state: Record<string, unknown>;
  orgs: { slug: string; name: string; role: string; url: string }[];
  login: { status: number; body: unknown };
  calls: { path: string; method: string; body: unknown }[];
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
  fake.calls.push({ path, method: init?.method ?? "GET", body });
  const route = fake.routes[path];
  if (route) return Promise.resolve(route(init?.method ?? "GET", body));
  if (path === "/api/portal/state") return Promise.resolve(json(fake.state));
  if (path === "/api/account/orgs") return Promise.resolve(json(fake.orgs));
  if (path === "/api/auth/login") return Promise.resolve(json(fake.login.body, fake.login.status));
  if (path === "/api/auth/reset") return Promise.resolve(json({ redirect: `${BASE}/orgs` }));
  if (path === "/api/projects") return Promise.resolve(json({ projects: [project("alpha")] }));
  if (path === "/api/projects/alpha") return Promise.resolve(json(project("alpha")));
  if (path === "/api/projects/alpha/scans") return Promise.resolve(json({ runs: [] }));
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

  it("has no /register any more (signed out: sign-in; signed in: the picker)", async () => {
    fake.state = baseState({ user: null });
    const out = mount("/register");
    await waitFor(() => expect(where(out)).toBe("/signin"));
    document.body.innerHTML = "";
    fake.state = baseState();
    const signedIn = mount("/register");
    await waitFor(() => expect(where(signedIn)).toBe("/orgs"));
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

  it("signed in on /signin with a valid next shows the session-not-received page (no loop)", async () => {
    const router = mount(`/signin?next=${encodeURIComponent(`${ORG}/p/alpha`)}`);
    await screen.findByRole("heading", { name: "Session not received" });
    expect(where(router)).toBe("/signin");
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /acme\.whygraph\.localhost:8765 again/ })).toHaveAttribute(
      "href",
      `${ORG}/p/alpha`,
    );
    expect(hard).not.toHaveBeenCalled();
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

  it("0 orgs goes to Create organization", async () => {
    const router = mount("/orgs");
    await waitFor(() => expect(where(router)).toBe("/orgs/new"));
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
    expect(screen.queryByRole("button", { name: "Scan now" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Chat" })).toBeNull();
  });

  it("a member sees Scan now and Chat", async () => {
    fake.state = orgState("member");
    mount("/p/alpha");
    await screen.findByRole("button", { name: "Scan now" });
    expect(screen.getByRole("link", { name: "Chat" })).toBeInTheDocument();
    expect(screen.queryByTestId("reader-banner")).toBeNull();
  });

  it("the Chat route shows a read-only notice", async () => {
    fake.state = orgState("reader", { user: adminAda });
    mount("/p/alpha/chat");
    await screen.findByTestId("chat-read-only");
  });

  it("hides the scan buttons on the Scans page", async () => {
    fake.state = orgState("reader", { user: adminAda });
    mount("/p/alpha/scans");
    await screen.findByRole("heading", { name: "Scans" });
    expect(screen.queryByRole("button", { name: "Scan now" })).toBeNull();
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
    await screen.findByText(/No rationale has been generated/);
    expect(screen.queryByRole("button", { name: "Generate rationale" })).toBeNull();
  });

  it("shows RationaleTab's Generate button to a member", async () => {
    rationale("member");
    await screen.findByRole("button", { name: "Generate rationale" });
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

  it("authMessage shows no_such_user verbatim and maps the new codes", () => {
    const words =
      "No one with that GitHub username has signed in to WhyGraph yet. Ask them to sign in once, then add them.";
    expect(authMessage(new ApiError(404, words, "no_such_user"))).toBe(words);
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

  it("adds by GitHub username and role", async () => {
    visit("admin");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("GitHub username"), "@dan");
    await user.selectOptions(screen.getByLabelText("Role"), "admin");
    await user.click(screen.getByRole("button", { name: "Add member" }));
    await screen.findByTestId("member-u9");
    const post = fake.calls.find((c) => c.path === "/api/org/members" && c.method === "POST");
    expect(post?.body).toEqual({ github_login: "@dan", role: "admin" });
  });

  it("shows no_such_user verbatim", async () => {
    const words =
      "No one with that GitHub username has signed in to WhyGraph yet. Ask them to sign in once, then add them.";
    visit("owner");
    serveMembers({ status: 404, body: { error: words, code: "no_such_user" } });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("GitHub username"), "nobody");
    await user.click(screen.getByRole("button", { name: "Add member" }));
    expect(await screen.findByTestId("add-member-error")).toHaveTextContent(words);
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
    await screen.findByRole("heading", { name: "Page not found" });
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

  it("disables and enables an account", async () => {
    mount("/admin");
    const u = userEvent.setup();
    const ben = within(await screen.findByTestId("admin-user-u2"));
    await u.click(ben.getByRole("button", { name: "Disable" }));
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
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
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
