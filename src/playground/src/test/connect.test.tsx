import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, setBaseUrl, type ConnectRequest } from "../api";
import { UseWithAgent } from "../components/portal/UseWithAgent";
import { LOCAL_PORT_KEY, localLinkUrl, parsePort, readStoredPort, revokedLabel } from "../lib/connections";
import { linkError } from "../lib/errors";
import { hardNavigate } from "../lib/navigation";
import { layerToValues, valuesToLayer, configFormSchema } from "../lib/configForm";
import { safeLinkNext } from "../lib/linkNext";
import { baseHostRedirect, createAppRouter, setupRedirect } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

const hard = vi.mocked(hardNavigate);

const BASE = "http://whygraph.localhost:8765";

const ada = { uid: "u1", display_name: "Ada", email: "ada@example.com", role: null, is_instance_admin: false };

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
function orgState(role: string) {
  return baseState({ host_kind: "org", org: { slug: "acme", name: "Acme", role } });
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// What the local portal put in the address.
const REQUEST: ConnectRequest = {
  redirect_uri: "http://127.0.0.1:8765/connect/callback",
  code_challenge: "c".repeat(43),
  code_challenge_method: "S256",
  state: "s".repeat(24),
  client_name: "my-laptop",
};
const query = (over: Record<string, string> = {}) => `?${new URLSearchParams({ ...REQUEST, ...over }).toString()}`;

// URLs only the server could have produced: a client that built its own would
// not know the marker, so the specs below can tell the two apart.
const SERVER_CANCEL = `${REQUEST.redirect_uri}?error=access_denied&state=${REQUEST.state}&iss=${encodeURIComponent(BASE)}&via=server-cancel`;
const SERVER_ALLOW = `${REQUEST.redirect_uri}?code=SERVERCODE&state=${REQUEST.state}&iss=${encodeURIComponent(BASE)}`;

interface Fake {
  state: Record<string, unknown>;
  calls: { path: string; method: string; body: unknown }[];
  routes: Record<string, (method: string, body: unknown) => Response>;
}
let fake: Fake;

const PROJECTS = [
  { org: "acme", org_name: "Acme", slug: "alpha", name: "Alpha", github_full_name: "acme/alpha", access_lost: false },
  { org: "acme", org_name: "Acme", slug: "beta", name: "Beta", github_full_name: "acme/beta", access_lost: true },
];

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = new URL(String(input), "http://127.0.0.1:8765").pathname;
  const method = init?.method ?? "GET";
  let body: unknown;
  try {
    body = init?.body ? JSON.parse(String(init.body)) : undefined;
  } catch {
    body = init?.body;
  }
  fake.calls.push({ path, method, body });
  const route = fake.routes[`${method} ${path}`];
  if (route) return Promise.resolve(route(method, body));
  if (path === "/api/portal/state") return Promise.resolve(json(fake.state));
  if (path === "/api/connect/validate")
    return Promise.resolve(
      json({
        ok: true,
        client_name: "my-laptop",
        port: 8765,
        org: null,
        project: null,
        orgs: [{ slug: "acme", name: "Acme", role: "member" }],
        cancel_url: SERVER_CANCEL,
      }),
    );
  if (path === "/api/connect/projects") return Promise.resolve(json(PROJECTS));
  if (path === "/api/connect/authorize") return Promise.resolve(json({ redirect: SERVER_ALLOW, access_lost: false }));
  return Promise.resolve(json({ error: "unhandled", detail: path }, 500));
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

beforeEach(() => {
  fake = { state: baseState(), calls: [], routes: {} };
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
});
afterEach(() => {
  vi.unstubAllGlobals();
  setBaseUrl(null);
});

// ---- the consent page ---------------------------------------------------------------

describe("connect page", () => {
  it("connect page uses server URLs only", async () => {
    const user = userEvent.setup();
    mount(`/connect${query()}`);
    await screen.findByTestId("connect-client");

    // Cancel goes to the server's `cancel_url`, byte for byte.
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(hard).toHaveBeenCalledTimes(1);
    expect(hard).toHaveBeenLastCalledWith(SERVER_CANCEL);

    // Allow goes to the server's `redirect`, byte for byte, after posting the validated request.
    await user.selectOptions(await screen.findByRole("combobox"), "acme/alpha");
    await user.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(hard).toHaveBeenCalledTimes(2));
    expect(hard).toHaveBeenLastCalledWith(SERVER_ALLOW);
    const authorize = fake.calls.find((c) => c.path === "/api/connect/authorize");
    expect(authorize?.body).toEqual({ ...REQUEST, org: "acme", project: "alpha" });
    // No navigation was to anything the page composed itself.
    for (const [url] of hard.mock.calls) expect([SERVER_CANCEL, SERVER_ALLOW]).toContain(url);
  });

  it("strips the query from the address once the session check passed", async () => {
    const router = mount(`/connect${query()}`);
    await screen.findByTestId("connect-client");
    await waitFor(() => expect(router.state.location.search).toEqual({}));
    expect(router.state.location.pathname).toBe("/connect");
    // A reload still has the request (sessionStorage), so the page does not die.
    expect(window.sessionStorage.getItem("whygraph.connect")).toContain("my-laptop");
  });

  it("shows no way forward for a request the server refuses", async () => {
    fake.routes["POST /api/connect/validate"] = () =>
      json({ detail: "redirect_uri must be exactly ...", code: "bad_connect_request", field: "redirect_uri" }, 422);
    mount(`/connect${query({ redirect_uri: "http://evil.example/connect/callback" })}`);
    const refused = await screen.findByTestId("connect-refused");
    expect(refused).toHaveTextContent("not valid");
    expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(hard).not.toHaveBeenCalled();
  });

  it("refuses an address with missing parts without asking the server", async () => {
    mount(`/connect?client_name=x`);
    await screen.findByTestId("connect-refused");
    expect(fake.calls.some((c) => c.path === "/api/connect/validate")).toBe(false);
  });

  it("says what to do when the user is a member of nothing", async () => {
    fake.routes["GET /api/connect/projects"] = () => json([]);
    mount(`/connect${query()}`);
    await screen.findByTestId("connect-no-projects");
    expect(screen.getByRole("button", { name: "Allow" })).toBeDisabled();
  });

  it("warns about an access-lost project and preselects the hinted one", async () => {
    fake.routes["POST /api/connect/validate"] = () =>
      json({
        ok: true,
        client_name: "my-laptop",
        port: 8765,
        org: "acme",
        project: "beta",
        orgs: [],
        cancel_url: SERVER_CANCEL,
      });
    mount(`/connect${query({ org: "acme", project: "beta" })}`);
    await screen.findByTestId("connect-access-lost");
    expect(screen.getByRole("combobox")).toHaveValue("acme/beta");
  });

  it("shows the server's refusal when Allow fails", async () => {
    fake.routes["POST /api/connect/authorize"] = () => json({ detail: "not found" }, 404);
    const user = userEvent.setup();
    mount(`/connect${query()}`);
    await user.selectOptions(await screen.findByRole("combobox"), "acme/alpha");
    await user.click(screen.getByRole("button", { name: "Allow" }));
    await screen.findByTestId("connect-error");
    expect(hard).not.toHaveBeenCalled();
  });
});

describe("router gates", () => {
  it("baseHostRedirect keeps /connect with next", () => {
    const href = `${BASE}/connect${query()}`;
    const out = baseHostRedirect(baseState({ user: null }) as never, "/connect", href);
    expect(out).toBe(`/signin?next=${encodeURIComponent(href)}`);
    // Signed in: rendered. Anything else signed out: plain /signin, no `next`.
    expect(baseHostRedirect(baseState() as never, "/connect", href)).toBeNull();
    expect(baseHostRedirect(baseState({ user: null }) as never, "/account", `${BASE}/account`)).toBe("/signin");
  });

  it("sends a signed-out /connect to /signin with its whole address as next", async () => {
    fake.state = baseState({ user: null });
    const router = mount(`/connect${query()}`);
    await waitFor(() => expect(router.state.location.pathname).toBe("/signin"));
    const next = (router.state.location.search as { next?: string }).next;
    expect(next).toBe(`http://localhost:3000/connect${query()}`.replace("http://localhost:3000", window.location.origin));
  });

  it("an org host sends /connect to the base host", async () => {
    fake.state = orgState("member");
    mount(`/connect${query()}`);
    await waitFor(() => expect(hard).toHaveBeenCalled());
    expect(String(hard.mock.calls[0][0])).toMatch(/^http:\/\/whygraph\.localhost:8765\/connect\?/);
  });

  it("setup gate keeps the link query", async () => {
    const search = "?platform=https%3A%2F%2Fwhy.example.com&org=acme&project=alpha";
    const out = setupRedirect({ setup_complete: false } as never, { pathname: "/link", search });
    expect(out).toBe(`/setup?next=${encodeURIComponent(`/link${search}`)}`);
    // Any other page just goes to /setup.
    expect(setupRedirect({ setup_complete: false } as never, { pathname: "/projects/new", search: "?a=b" })).toBe("/setup");
    // After setup, /setup resumes the link request - and only a /link one.
    const nextSearch = `?next=${encodeURIComponent(`/link${search}`)}`;
    expect(setupRedirect({ setup_complete: true } as never, { pathname: "/setup", search: nextSearch })).toBe(`/link${search}`);
    expect(safeLinkNext(`?next=${encodeURIComponent("/p/alpha")}`)).toBeNull();
    expect(safeLinkNext(`?next=${encodeURIComponent("//evil.example/link")}`)).toBeNull();
    expect(setupRedirect({ setup_complete: true } as never, { pathname: "/setup" })).toBe("/");
  });

  it("setup gate keeps the link query through the router", async () => {
    fake.state = { mode: "local", setup_complete: false, user: null };
    const router = mount("/link?platform=x&org=acme&project=alpha");
    await waitFor(() => expect(router.state.location.pathname).toBe("/setup"));
    expect((router.state.location.search as { next?: string }).next).toBe("/link?platform=x&org=acme&project=alpha");
  });
});

// ---- account and project settings ------------------------------------------------------

const MINE = [
  {
    uid: "t1",
    org: "acme",
    project: "alpha",
    project_name: "Alpha",
    client_name: "my-laptop",
    created_at: "2026-01-01T00:00:00Z",
    last_used_at: null,
    revoked_at: null,
    revoked_reason: null,
  },
  {
    uid: "t2",
    org: "acme",
    project: "beta",
    project_name: "Beta",
    client_name: "old-desktop",
    created_at: "2026-01-01T00:00:00Z",
    last_used_at: null,
    revoked_at: "2026-02-01T00:00:00Z",
    revoked_reason: "admin_revoked",
  },
];

describe("connected portals", () => {
  it("lists the account's own tokens and revokes a live one", async () => {
    let rows = MINE;
    fake.routes["GET /api/account"] = () =>
      json({ uid: "u1", display_name: "Ada", email: "ada@example.com", github_login: null, avatar_url: null, has_password: false });
    fake.routes["GET /api/connect/tokens"] = () => json(rows);
    fake.routes["DELETE /api/connect/tokens/t1"] = () => {
      rows = [{ ...MINE[0], revoked_at: "2026-03-01T00:00:00Z", revoked_reason: "user_revoked" } as never, MINE[1]];
      return new Response(null, { status: 204 });
    };
    const user = userEvent.setup();
    mount("/account");
    const section = await screen.findByTestId("my-connections");
    await waitFor(() => expect(section.querySelectorAll("[data-testid=connection-row]")).toHaveLength(2));
    expect(section).toHaveTextContent("Revoked by an admin");
    expect(section.querySelectorAll("button")).toHaveLength(1); // only the live one can be revoked
    await user.click(section.querySelector("button")!);
    await waitFor(() => expect(section).toHaveTextContent("Revoked by you"));
  });

  it("lists a project's connections for an admin, with the member and the machine", async () => {
    fake.state = orgState("admin");
    fake.routes["GET /api/projects/alpha"] = () =>
      json({
        slug: "alpha",
        name: "Alpha",
        source: "github",
        root: "/r/alpha",
        remote_url: null,
        initialized: true,
        initialized_at: null,
        last_scan_at: null,
        created_at: "2026-01-01T00:00:00Z",
        root_status: "ok",
        running_scan: null,
        stale: null,
        agents: [],
        missing_key: null,
        mcp_url: null,
        stats: null,
      });
    fake.routes["GET /api/projects/alpha/connections"] = () =>
      json([{ uid: "c1", user_login: "grace", user_name: "Grace", client_name: "grace-mbp", created_at: "2026-01-01T00:00:00Z", last_used_at: null }]);
    fake.routes["GET /api/projects/alpha/config"] = () =>
      json({ config: {}, secrets: { llm: {}, github_token: { set: false, hint: null } }, import: null });
    fake.routes["GET /api/portal/defaults"] = () =>
      json({ config: {}, secrets: { llm: {}, github_token: { set: false, hint: null } }, no_provider_key: false });
    fake.routes["GET /api/projects"] = () => json({ projects: [] });
    mount("/p/alpha/settings");
    const section = await screen.findByTestId("project-connections");
    await waitFor(() => expect(section).toHaveTextContent("grace-mbp"));
    expect(section).toHaveTextContent("Grace");
    expect(section).toHaveTextContent("@grace");
    expect(screen.getByRole("button", { name: "Revoke" })).toBeEnabled();

    // A plain member does not get the section at all.
    document.body.innerHTML = "";
    fake.state = orgState("member");
    mount("/p/alpha/settings");
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.queryByTestId("project-connections")).toBeNull();
  });
});

// ---- org settings: the two agent limits ---------------------------------------------------

describe("agent limits", () => {
  const defaults = (config: Record<string, unknown> = {}) =>
    json({ config, secrets: { llm: {}, github_token: { set: false, hint: null } }, no_provider_key: false });

  it("an owner edits both limits on the org defaults and saves them as numbers", async () => {
    fake.state = orgState("owner");
    let put: { config?: Record<string, Record<string, unknown>> } | undefined;
    fake.routes["GET /api/portal/defaults"] = () => defaults({ rationale: { agent_generations_per_hour: 50 } });
    fake.routes["PUT /api/portal/defaults"] = (_m, body) => {
      put = body as typeof put;
      return defaults();
    };
    fake.routes["GET /api/projects"] = () => json({ projects: [] });
    const user = userEvent.setup();
    mount("/settings");
    const generations = await screen.findByLabelText("Rationale cards per hour");
    expect(generations).toHaveValue("50");
    await user.type(screen.getByLabelText("Commit descriptions per hour"), "300");
    await user.clear(generations);
    await user.type(generations, "0");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toBeDefined());
    expect(put?.config?.rationale).toEqual({ agent_generations_per_hour: 0 });
    expect(put?.config?.analyze).toEqual({ agent_descriptions_per_hour: 300 });
  });

  it("rejects a value outside 0 to 10,000", async () => {
    fake.state = orgState("owner");
    fake.routes["GET /api/portal/defaults"] = () => defaults();
    fake.routes["GET /api/projects"] = () => json({ projects: [] });
    const user = userEvent.setup();
    mount("/settings");
    await user.type(await screen.findByLabelText("Rationale cards per hour"), "10001");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/whole number from 0 to 10,000/);
    expect(fake.calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("is hidden from everyone but an owner", async () => {
    fake.state = orgState("admin");
    fake.routes["GET /api/portal/defaults"] = () => defaults();
    fake.routes["GET /api/projects"] = () => json({ projects: [] });
    mount("/settings");
    await screen.findByTestId("config-form");
    expect(screen.queryByLabelText("Rationale cards per hour")).toBeNull();
  });

  it("the form helpers never put the org-only keys in a project layer", () => {
    const base = { rationale: { agent_generations_per_hour: 5 } };
    const values = { ...layerToValues(base), agentGenerations: "99" };
    // Project scope (no `limits`): the stored layer is passed through untouched.
    expect(valuesToLayer(base, values, { scan: true }).rationale).toEqual({ agent_generations_per_hour: 5 });
    // Defaults scope: the typed value wins, and blank drops the key.
    expect(valuesToLayer(base, values, { scan: false, limits: true }).rationale).toEqual({
      agent_generations_per_hour: 99,
    });
    expect(valuesToLayer(base, { ...values, agentGenerations: "" }, { scan: false, limits: true }).rationale).toBeUndefined();
    expect(configFormSchema.shape.agentGenerations.safeParse("12x").success).toBe(false);
  });
});

// ---- "Use with your agent" ---------------------------------------------------------------

describe("Use with your agent", () => {
  it("links to the local portal's /link page with the platform, org and project", () => {
    render(<UseWithAgent baseUrl={`${BASE}/`} org="acme" slug="alpha" />);
    const link = screen.getByRole("link", { name: "Open in my local WhyGraph" });
    const url = new URL(link.getAttribute("href")!);
    expect(url.origin).toBe("http://127.0.0.1:8765");
    expect(url.pathname).toBe("/link");
    expect(Object.fromEntries(url.searchParams)).toEqual({ platform: BASE, org: "acme", project: "alpha" });
    expect(screen.getByText(/Nothing opened\?/)).toHaveTextContent("whygraph up");
  });

  it("remembers the port, and falls back when storage throws", async () => {
    const user = userEvent.setup();
    render(<UseWithAgent baseUrl={BASE} org="acme" slug="alpha" />);
    const port = screen.getByLabelText("Your local portal's port");
    await user.clear(port);
    await user.type(port, "9100");
    expect(window.localStorage.getItem(LOCAL_PORT_KEY)).toBe("9100");
    expect(screen.getByRole("link", { name: "Open in my local WhyGraph" })).toHaveAttribute(
      "href",
      localLinkUrl(9100, BASE, "acme", "alpha"),
    );
    await user.clear(port);
    await user.type(port, "abc");
    expect(screen.queryByRole("link", { name: "Open in my local WhyGraph" })).toBeNull();
    expect(window.localStorage.getItem(LOCAL_PORT_KEY)).toBe("9100"); // an invalid port is not kept

    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readStoredPort()).toBe("8765");
    get.mockRestore();
    expect(parsePort("0")).toBeNull();
    expect(parsePort("65535")).toBe(65535);
  });

  it("makes no request to the local portal", () => {
    render(<UseWithAgent baseUrl={BASE} org="acme" slug="alpha" />);
    expect(fetch).not.toHaveBeenCalled();
  });
});

// ---- messages --------------------------------------------------------------------------------

describe("link error messages", () => {
  const codes = [
    "managed_on_platform",
    "slug_taken",
    "origin_mismatch",
    "connect_expired",
    "link_expired",
    "issuer_mismatch",
    "access_denied",
    "bad_platform_url",
    "bad_platform_reply",
    "bad_connect_request",
    "invalid_token",
    "token_revoked",
    "generation_limited",
    "generation_disabled",
    "no_llm_key",
    "llm_unavailable",
    "busy",
  ];
  it("has a human sentence for every code of the plan", () => {
    for (const code of codes) {
      const message = linkError(new ApiError(400, "raw server text", code));
      expect(message, code).not.toBe("raw server text");
      expect(message, code).not.toContain("_");
    }
  });
  it("falls back to the server's own message for an unknown code", () => {
    expect(linkError(new ApiError(500, "boom", "weird"))).toBe("boom");
    expect(linkError(new ApiError(500, "boom", "constructor"))).toBe("boom");
  });
  it("labels every revocation reason", () => {
    for (const r of ["user_revoked", "admin_revoked", "removed_locally", "member_removed", "member_left", "user_disabled", "project_deleted", "org_deleted", "idle", "project_access_removed"]) {
      expect(revokedLabel(r)).not.toBe("Revoked");
    }
    expect(revokedLabel(null)).toBe("Revoked");
  });
});
