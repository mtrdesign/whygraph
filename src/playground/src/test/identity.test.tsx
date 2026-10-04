import { render, screen, waitFor } from "@testing-library/react";
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

interface Fake {
  state: Record<string, unknown>;
  orgs: { slug: string; name: string; role: string; url: string }[];
  login: { status: number; body: unknown };
  calls: { path: string; method: string; body: unknown }[];
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

  it("lets a signed-out visitor open /register and /reset", async () => {
    fake.state = baseState({ user: null });
    const router = mount("/register");
    await screen.findByRole("heading", { name: /create.*account|register/i });
    expect(where(router)).toBe("/register");
  });

  it("sends / and /register to the picker when signed in", async () => {
    for (const path of ["/", "/register"]) {
      const router = mount(path);
      await waitFor(() => expect(where(router)).toBe("/orgs"));
      document.body.innerHTML = "";
    }
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
  it("posts the credentials with next, then follows the server's redirect", async () => {
    fake.state = baseState({ user: null });
    fake.login = { status: 200, body: { redirect: `${ORG}/` } };
    const next = `${ORG}/p/alpha`;
    mount(`/signin?next=${encodeURIComponent(next)}`);
    const user = userEvent.setup();
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
    await user.type(await screen.findByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Incorrect email or password.");
    expect(hard).not.toHaveBeenCalled();
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

  it("authMessage maps the password codes inline", () => {
    expect(authMessage(new ApiError(422, "x", "weak_password"))).toMatch(/15 characters/);
    expect(authMessage(new ApiError(422, "x", "common_password"))).toMatch(/too common/);
    expect(authMessage(new ApiError(429, "slow"))).toMatch(/Too many/);
  });
});
