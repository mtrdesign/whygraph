import { StrictMode } from "react";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { PROJECT_ACTIONS } from "../lib/permissions";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, portalKey, setBaseUrl, type ConnectRequest } from "../api";
import { UseWithAgent } from "../components/portal/UseWithAgent";
import { LOCAL_PORT_KEY, localLinkUrl, parsePort, readStoredPort, revokedLabel } from "../lib/connections";
import { linkError } from "../lib/errors";
import { hardNavigate } from "../lib/navigation";
import { layerToValues, valuesToLayer, configFormSchema } from "../lib/configForm";
import { safeLinkNext } from "../lib/linkNext";
import { complete } from "../pages/ConnectPage";
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
  if (path === "/api/projects/alpha/connections/mine") return Promise.resolve(json([]));
  if (path === "/api/connect/projects") return Promise.resolve(json(PROJECTS));
  if (path === "/api/connect/authorize") return Promise.resolve(json({ redirect: SERVER_ALLOW, access_lost: false }));
  return Promise.resolve(json({ error: "unhandled", detail: path }, 500));
}

function mount(path: string, { strict = false }: { strict?: boolean } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
  const router = createAppRouter({ queryClient, history: createMemoryHistory({ initialEntries: [path] }) });
  const app = (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>
  );
  render(strict ? <StrictMode>{app}</StrictMode> : app);
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

/** Choose a project in the consent page's styled select (CN-5: `ui/select`, not a native `<select>`). */
async function pickProject(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(await screen.findByRole("combobox", { name: "Project" }));
  await user.click(await screen.findByRole("option", { name: label }));
}

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
    await pickProject(user, "Acme / Alpha");
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
    // The styled select shows the hinted project (CN-5).
    expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Acme / Beta");
  });

  it("shows the server's refusal when Allow fails", async () => {
    fake.routes["POST /api/connect/authorize"] = () => json({ detail: "not found" }, 404);
    const user = userEvent.setup();
    mount(`/connect${query()}`);
    await pickProject(user, "Acme / Alpha");
    await user.click(screen.getByRole("button", { name: "Allow" }));
    await screen.findByTestId("connect-error");
    expect(hard).not.toHaveBeenCalled();
  });
});

describe("pre-approved consent", () => {
  const INTENT = "i".repeat(43);
  const HINTED = { org: "acme", project: "alpha" };
  const validated = (over: Record<string, unknown> = {}) =>
    json({
      ok: true,
      client_name: "my-laptop",
      port: 8765,
      ...HINTED,
      orgs: [{ slug: "acme", name: "Acme", role: "member" }],
      cancel_url: SERVER_CANCEL,
      pre_approved: true,
      ...over,
    });
  const authorizes = () => fake.calls.filter((c) => c.path === "/api/connect/authorize");
  const EXPIRED = "This one-time approval expired or was already used. Approve this connection below.";

  it("submits once by itself on pre_approved and never shows Allow, even under StrictMode", async () => {
    fake.state = baseState({ user: { ...ada, github_login: "ada" } });
    fake.routes["POST /api/connect/validate"] = () => validated();
    let sawAllow = false;
    const watch = new MutationObserver(() => {
      if (Array.from(document.querySelectorAll("button")).some((b) => b.textContent === "Allow")) sawAllow = true;
    });
    watch.observe(document.body, { childList: true, subtree: true, characterData: true });
    mount(`/connect${query({ ...HINTED, intent: INTENT })}`, { strict: true });

    const auto = await screen.findByTestId("connect-auto");
    expect(auto).toHaveAttribute("role", "status");
    expect(auto).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("heading", { name: "Connecting my-laptop…" })).toBeInTheDocument();
    await waitFor(() => expect(hard).toHaveBeenCalledWith(SERVER_ALLOW));
    expect(screen.getByTestId("connect-auto")).toHaveTextContent("to Acme / Alpha as @ada");
    await new Promise((r) => setTimeout(r, 50));
    watch.disconnect();
    expect(authorizes()).toHaveLength(1);
    expect(authorizes()[0].body).toEqual({ ...REQUEST, ...HINTED, intent: INTENT });
    expect(hard).toHaveBeenCalledTimes(1);
    expect(sawAllow).toBe(false);
    expect(window.sessionStorage.getItem("whygraph.connect.done")).toBe("1");
    expect(window.sessionStorage.getItem("whygraph.connect")).toBeNull();
  });

  it("falls back to Allow on intent_expired with a notice, focus on Allow, and no intent on the manual Allow", async () => {
    let n = 0;
    fake.routes["POST /api/connect/validate"] = () => validated();
    fake.routes["POST /api/connect/authorize"] = () =>
      ++n === 1
        ? json({ detail: "this connection needs your approval", code: "intent_expired" }, 409)
        : json({ redirect: SERVER_ALLOW, access_lost: false });
    const user = userEvent.setup();
    mount(`/connect${query({ ...HINTED, intent: INTENT })}`);
    expect(await screen.findByTestId("connect-intent-expired")).toHaveTextContent(EXPIRED);
    expect(screen.queryByTestId("connect-error")).toBeNull();
    expect(screen.queryByTestId("connect-auto")).toBeNull();
    const allow = screen.getByRole("button", { name: "Allow" });
    await waitFor(() => expect(allow).toHaveFocus());
    expect(authorizes()).toHaveLength(1);
    expect(authorizes()[0].body).toMatchObject({ intent: INTENT });
    expect(hard).not.toHaveBeenCalled();

    await user.click(allow);
    await waitFor(() => expect(hard).toHaveBeenCalledWith(SERVER_ALLOW));
    expect(authorizes()).toHaveLength(2);
    expect(authorizes()[1].body).toEqual({ ...REQUEST, ...HINTED });
  });

  it("another auto failure keeps the error alert, and Allow then sends no intent", async () => {
    let n = 0;
    fake.routes["POST /api/connect/validate"] = () => validated();
    fake.routes["POST /api/connect/authorize"] = () =>
      ++n === 1 ? json({ detail: "boom" }, 500) : json({ redirect: SERVER_ALLOW, access_lost: false });
    const user = userEvent.setup();
    mount(`/connect${query({ ...HINTED, intent: INTENT })}`);
    await screen.findByTestId("connect-error");
    expect(screen.queryByTestId("connect-intent-expired")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(hard).toHaveBeenCalledWith(SERVER_ALLOW));
    expect(authorizes()).toHaveLength(2);
    expect(authorizes()[1].body).not.toHaveProperty("intent");
  });

  it("a manual Allow never sends the intent when the server did not vouch for it", async () => {
    fake.routes["POST /api/connect/validate"] = () => validated({ pre_approved: false });
    const user = userEvent.setup();
    mount(`/connect${query({ ...HINTED, intent: INTENT })}`);
    const allow = await screen.findByRole("button", { name: "Allow" });
    expect(screen.queryByTestId("connect-auto")).toBeNull();
    await new Promise((r) => setTimeout(r, 50));
    expect(authorizes()).toEqual([]);
    await user.click(allow);
    await waitFor(() => expect(hard).toHaveBeenCalledWith(SERVER_ALLOW));
    expect(authorizes()).toHaveLength(1);
    expect(authorizes()[0].body).toEqual({ ...REQUEST, ...HINTED });
  });

  it("shows the normal page when the hinted project is not in the list", async () => {
    fake.routes["POST /api/connect/validate"] = () => validated({ project: "gamma" });
    mount(`/connect${query({ org: "acme", project: "gamma", intent: INTENT })}`);
    expect(await screen.findByRole("button", { name: "Allow" })).toBeInTheDocument();
    expect(screen.queryByTestId("connect-auto")).toBeNull();
    await new Promise((r) => setTimeout(r, 50));
    expect(authorizes()).toEqual([]);
  });

  it("keeps the intent through the sessionStorage round trip", async () => {
    mount(`/connect${query({ ...HINTED, intent: INTENT })}`);
    await screen.findByTestId("connect-client");
    expect(JSON.parse(window.sessionStorage.getItem("whygraph.connect")!)).toMatchObject({ intent: INTENT });
    cleanup();
    fake.calls.length = 0;
    // A reload after the address was cleaned (or the sign-in round trip) reads the saved copy.
    mount("/connect");
    await screen.findByTestId("connect-client");
    expect(fake.calls.find((c) => c.path === "/api/connect/validate")?.body).toMatchObject({ intent: INTENT });
    expect(complete({ ...REQUEST, intent: INTENT })).toEqual({ ...REQUEST, intent: INTENT });
    expect(complete({ ...REQUEST })).not.toHaveProperty("intent");
  });

  it("Back after a finished consent says so instead of refused, and a new request forgets it", async () => {
    window.sessionStorage.setItem("whygraph.connect.done", "1");
    mount("/connect");
    expect(await screen.findByRole("heading", { name: "Connection finished" })).toBeInTheDocument();
    expect(screen.getByText("You can close this tab.")).toBeInTheDocument();
    expect(screen.queryByTestId("connect-refused")).toBeNull();
    expect(fake.calls.some((c) => c.path === "/api/connect/validate")).toBe(false);
    cleanup();
    mount(`/connect${query()}`);
    await screen.findByTestId("connect-client");
    expect(window.sessionStorage.getItem("whygraph.connect.done")).toBeNull();
  });

  it("reloads a page restored from the back-forward cache", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    mount(`/connect${query()}`);
    await screen.findByTestId("connect-client");
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: false }));
    expect(reload).not.toHaveBeenCalled();
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

describe("router gates", () => {
  it("/connect keeps intent in its search and the page sends it to validate", async () => {
    const INTENT = "x".repeat(43);
    mount(`/connect${query({ intent: INTENT })}`);
    await screen.findByTestId("connect-client");
    expect(fake.calls.find((c) => c.path === "/api/connect/validate")?.body).toEqual({ ...REQUEST, intent: INTENT });
  });

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
    // A confirmation first; the row then says why, with no toast.
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("That machine loses access at once");
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));
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
        restricted: false,
        // The role follows the org role: a plain member is a contributor.
        my_role: (fake.state.org as { role: string }).role === "member" ? "contributor" : "admin",
        permissions:
          (fake.state.org as { role: string }).role === "member"
            ? ["project.read", "project.chat", "project.scan"]
            : PROJECT_ACTIONS,
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
    await user.click(within(screen.getByRole("region", { name: "Agent limits" })).getByRole("button", { name: "Save" }));
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
    await user.click(within(screen.getByRole("region", { name: "Agent limits" })).getByRole("button", { name: "Save" }));
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

const MINE_PATH = "/api/projects/alpha/connections/mine";
const machine = (over: Record<string, unknown> = {}) => ({
  uid: "m1",
  client_name: "my-laptop",
  created_at: "2026-01-01T00:00:00Z",
  last_used_at: "2026-01-02T00:00:00Z",
  ...over,
});
const LINK = { name: "Open in my local WhyGraph" };
const INTENT_PATH = "/api/projects/alpha/connect-intent";
const INTENT_A = "a".repeat(43);
const INTENT_B = "b".repeat(43);

/** `UseWithAgent` needs a query client (the "mine" list); the app defaults are kept. */
function renderCard(props: Partial<React.ComponentProps<typeof UseWithAgent>> = {}, seed?: (qc: QueryClient) => void) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
  seed?.(queryClient);
  const view = render(
    <QueryClientProvider client={queryClient}>
      <UseWithAgent baseUrl={BASE} org="acme" slug="alpha" repo="acme/alpha" {...props} />
    </QueryClientProvider>,
  );
  return { queryClient, ...view };
}

describe("Use with your agent", () => {
  it("links to the local portal's /link page with the platform, org and project", async () => {
    renderCard({ baseUrl: `${BASE}/` });
    const link = await screen.findByRole("link", LINK);
    const url = new URL(link.getAttribute("href")!);
    expect(url.origin).toBe("http://127.0.0.1:8765");
    expect(url.pathname).toBe("/link");
    expect(Object.fromEntries(url.searchParams)).toEqual({ platform: BASE, org: "acme", project: "alpha" });
    expect(screen.getByText(/If the new tab says the site can't be reached/)).toHaveTextContent("whygraph up");
    expect(screen.getByText(/On another port\?/)).toHaveTextContent("whygraph status");
    expect(screen.getByTestId("local-port-summary")).toHaveTextContent("Port 8765 · Change");
    expect(screen.queryByLabelText("Port of your local WhyGraph")).toBeNull();
  });

  it("names the repository to clone, and shares its folder in the one start step", async () => {
    renderCard();
    await screen.findByRole("link", LINK);
    expect(screen.getByTestId("clone-step")).toHaveTextContent(
      "Clone acme/alpha into the folder where you keep your repositories, if it is not there yet.",
    );
    expect(screen.getByText(/Start WhyGraph with that folder shared:/)).toHaveTextContent("whygraph up --add-folder ~/Work");
    expect(screen.getByText("Press the button below, then pick that clone on your machine.")).toBeInTheDocument();
  });

  it("reads 'this repository' when the project has no repository name", async () => {
    renderCard({ repo: null });
    await screen.findByRole("link", LINK);
    expect(screen.getByTestId("clone-step")).toHaveTextContent("Clone this repository into the folder");
  });

  it("changes and remembers the port with the keyboard, and falls back when storage throws", async () => {
    const user = userEvent.setup();
    renderCard();
    await screen.findByRole("link", LINK);
    await user.click(screen.getByRole("button", { name: "Change" }));
    const port = screen.getByLabelText("Port of your local WhyGraph");
    expect(port).toHaveFocus();
    await user.clear(port);
    await user.type(port, "9100{Enter}");
    expect(window.localStorage.getItem(LOCAL_PORT_KEY)).toBe("9100");
    expect(screen.getByRole("link", LINK)).toHaveAttribute("href", localLinkUrl(9100, BASE, "acme", "alpha"));
    expect(screen.queryByLabelText("Port of your local WhyGraph")).toBeNull();
    expect(screen.getByRole("button", { name: "Change" })).toHaveFocus();
    expect(screen.getByTestId("local-port-summary")).toHaveTextContent("Port 9100");

    // An invalid draft shows the error, keeps the last committed port, and is not stored.
    await user.click(screen.getByRole("button", { name: "Change" }));
    const again = screen.getByLabelText("Port of your local WhyGraph");
    await user.clear(again);
    await user.type(again, "abc{Enter}");
    expect(again).toHaveAttribute("aria-describedby", "local-port-error");
    expect(screen.getByText("Enter a port from 1 to 65535.")).toBeInTheDocument();
    expect(screen.getByRole("link", LINK)).toHaveAttribute("href", localLinkUrl(9100, BASE, "acme", "alpha"));
    expect(window.localStorage.getItem(LOCAL_PORT_KEY)).toBe("9100");

    // Escape reverts and returns to Change.
    await user.keyboard("{Escape}");
    expect(screen.queryByLabelText("Port of your local WhyGraph")).toBeNull();
    expect(screen.getByRole("button", { name: "Change" })).toHaveFocus();
    expect(screen.getByTestId("local-port-summary")).toHaveTextContent("Port 9100");

    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readStoredPort()).toBe("8765");
    get.mockRestore();
    expect(parsePort("0")).toBeNull();
    expect(parsePort("65535")).toBe(65535);
  });

  it("commits the port on blur and starts from the remembered one", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(LOCAL_PORT_KEY, "5174");
    renderCard();
    expect(await screen.findByTestId("local-port-summary")).toHaveTextContent("Port 5174");
    await user.click(screen.getByRole("button", { name: "Change" }));
    const port = screen.getByLabelText("Port of your local WhyGraph");
    await user.clear(port);
    await user.type(port, "9200");
    await user.tab();
    expect(screen.getByRole("link", LINK)).toHaveAttribute("href", localLinkUrl(9200, BASE, "acme", "alpha"));
    expect(window.localStorage.getItem(LOCAL_PORT_KEY)).toBe("9200");
  });

  it("makes no request whose host is 127.0.0.1", async () => {
    renderCard();
    await screen.findByRole("link", LINK);
    const urls = vi.mocked(fetch).mock.calls.map(([u]) => String(u));
    expect(urls.some((u) => u.endsWith(MINE_PATH))).toBe(true);
    // Our own API calls are relative; none may name the local portal's host.
    expect(urls.filter((u) => /^https?:/.test(u) && new URL(u).hostname === "127.0.0.1")).toEqual([]);
  });

  it("shows the steps when no machine is linked, and a machine with a closed disclosure when one is", async () => {
    const user = userEvent.setup();
    fake.routes[`GET ${MINE_PATH}`] = () => json([machine()]);
    renderCard();
    const rows = await screen.findAllByTestId("my-machine-row");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("Connected on my-laptop");
    expect(rows[0]).toHaveTextContent("linked");
    expect(rows[0]).toHaveTextContent("last used");
    expect(rows[0]).not.toHaveAttribute("data-state");
    expect(within(rows[0]).getByRole("link", { name: "Manage in Account" })).toHaveAttribute("href", `${BASE}/account`);
    // A closed disclosure renders no steps (so nothing inside can mint or fetch).
    const another = screen.getByTestId("connect-another");
    expect(another).toHaveTextContent("Connect another machine");
    expect(screen.queryByRole("link", LINK)).toBeNull();
    expect(screen.queryByTestId("local-port-summary")).toBeNull();
    await user.click(within(another).getByText("Connect another machine"));
    expect(await screen.findByRole("link", LINK)).toBeInTheDocument();
  });

  it("lists two machines, newest first as served", async () => {
    fake.routes[`GET ${MINE_PATH}`] = () =>
      json([machine({ uid: "m2", client_name: "desktop" }), machine({ uid: "m1", client_name: "laptop" })]);
    renderCard();
    const rows = await screen.findAllByTestId("my-machine-row");
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("Connected on desktop"),
      expect.stringContaining("Connected on laptop"),
    ]);
    expect(screen.getByTestId("connect-another")).toBeInTheDocument();
  });

  it("reads a never-used machine as approved, not linked yet, and keeps the steps open", async () => {
    fake.routes[`GET ${MINE_PATH}`] = () => json([machine({ last_used_at: null, client_name: "a-very-long-hostname.local" })]);
    renderCard();
    const row = await screen.findByTestId("my-machine-row");
    expect(row).toHaveAttribute("data-state", "pending");
    expect(row).toHaveTextContent("Approved for a-very-long-hostname.local");
    expect(row).toHaveTextContent("not linked yet - finish on that machine");
    expect(row.firstElementChild!.className).toContain("break-words");
    expect(row.firstElementChild!.className).toContain("min-w-0");
    expect(await screen.findByRole("link", LINK)).toBeInTheDocument();
    expect(screen.queryByTestId("connect-another")).toBeNull();
  });

  it("treats a failed list as no machine", async () => {
    fake.routes[`GET ${MINE_PATH}`] = () => json({ error: "boom" }, 500);
    renderCard();
    expect(await screen.findByRole("link", LINK)).toBeInTheDocument();
    expect(screen.queryByTestId("my-machines")).toBeNull();
  });

  it("shows the machine after the tab regains focus, without a reload", async () => {
    renderCard();
    await screen.findByRole("link", LINK);
    expect(screen.queryByTestId("my-machines")).toBeNull();
    fake.routes[`GET ${MINE_PATH}`] = () => json([machine()]);
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    expect(await screen.findByTestId("my-machines")).toHaveTextContent("Connected on my-laptop");
    focusManager.setFocused(undefined);
  });

  it("variant=dialog renders the steps with no card title", async () => {
    renderCard({ variant: "dialog" });
    expect(await screen.findByRole("link", LINK)).toBeInTheDocument();
    expect(screen.queryByTestId("use-with-agent")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Connect local WhyGraph" })).toBeNull();
  });

  it("variant=card has the title; variant=embedded is only the steps and asks for nothing", async () => {
    const card = renderCard();
    expect(await screen.findByRole("heading", { name: "Connect local WhyGraph" })).toBeInTheDocument();
    card.unmount();
    fake.calls.length = 0;
    renderCard({ variant: "embedded" });
    expect(screen.getByRole("link", LINK)).toBeInTheDocument();
    expect(screen.queryByTestId("use-with-agent")).toBeNull();
    expect(fake.calls.some((c) => c.path === MINE_PATH)).toBe(false);
  });

  it("puts the minted intent in the link and says what the click grants", async () => {
    const user = userEvent.setup();
    fake.routes[`POST ${INTENT_PATH}`] = (_m, body) =>
      json({ intent: (body as { port: number }).port === 9100 ? INTENT_B : INTENT_A, expires_in: 900 });
    renderCard();
    const link = await screen.findByRole("link", LINK);
    await waitFor(() => expect(link).toHaveAttribute("href", localLinkUrl(8765, BASE, "acme", "alpha", INTENT_A)));
    expect(new URL(link.getAttribute("href")!).searchParams.get("intent")).toBe(INTENT_A);
    expect(fake.calls.find((c) => c.path === INTENT_PATH)).toMatchObject({ method: "POST", body: { port: 8765 } });
    expect(screen.getByTestId("connect-scope")).toHaveTextContent(
      "This lets WhyGraph on port 8765 of this computer read this project as you and, if your role allows, ask for rationale cards with your organization's AI keys. You confirm once more on your machine.",
    );
    // The intent is bound to the port: a new port gets its own.
    await user.click(screen.getByRole("button", { name: "Change" }));
    const port = screen.getByLabelText("Port of your local WhyGraph");
    await user.clear(port);
    await user.type(port, "9100{Enter}");
    await waitFor(() =>
      expect(screen.getByRole("link", LINK)).toHaveAttribute("href", localLinkUrl(9100, BASE, "acme", "alpha", INTENT_B)),
    );
    expect(fake.calls.filter((c) => c.path === INTENT_PATH).map((c) => c.body)).toEqual([{ port: 8765 }, { port: 9100 }]);
    expect(screen.getByTestId("connect-scope")).toHaveTextContent("port 9100 of this computer");
  });

  it("leaves the intent out of the link when the mint fails", async () => {
    fake.routes[`POST ${INTENT_PATH}`] = () => json({ detail: "slow down", code: "throttled" }, 429);
    renderCard();
    const link = await screen.findByRole("link", LINK);
    await waitFor(() => expect(fake.calls.some((c) => c.path === INTENT_PATH)).toBe(true));
    await new Promise((r) => setTimeout(r, 20));
    expect(link).toHaveAttribute("href", localLinkUrl(8765, BASE, "acme", "alpha"));
    expect(new URL(link.getAttribute("href")!).searchParams.has("intent")).toBe(false);
  });

  it("a click asks for a fresh intent, since each is single use", async () => {
    let n = 0;
    fake.routes[`POST ${INTENT_PATH}`] = () => json({ intent: ++n === 1 ? INTENT_A : INTENT_B, expires_in: 900 });
    renderCard();
    const link = await screen.findByRole("link", LINK);
    await waitFor(() => expect(link).toHaveAttribute("href", localLinkUrl(8765, BASE, "acme", "alpha", INTENT_A)));
    link.addEventListener("click", (e) => e.preventDefault()); // jsdom does not open tabs
    await userEvent.setup().click(link);
    await waitFor(() => expect(link).toHaveAttribute("href", localLinkUrl(8765, BASE, "acme", "alpha", INTENT_B)));
    expect(fake.calls.filter((c) => c.path === INTENT_PATH)).toHaveLength(2);
  });

  it("a closed Connect another machine mints no intent until it is opened", async () => {
    const user = userEvent.setup();
    fake.routes[`GET ${MINE_PATH}`] = () => json([machine()]);
    fake.routes[`POST ${INTENT_PATH}`] = () => json({ intent: INTENT_A, expires_in: 900 });
    renderCard();
    const another = await screen.findByTestId("connect-another");
    await new Promise((r) => setTimeout(r, 20));
    expect(fake.calls.some((c) => c.path === INTENT_PATH)).toBe(false);
    await user.click(within(another).getByText("Connect another machine"));
    await waitFor(() => expect(fake.calls.some((c) => c.path === INTENT_PATH)).toBe(true));
  });

  it("a reader makes no request for the machine list and mints no intent", async () => {
    renderCard({}, (qc) => qc.setQueryData(portalKey("state"), orgState("reader")));
    expect(await screen.findByRole("link", LINK)).toBeInTheDocument();
    expect(fake.calls).toEqual([]);
    expect(screen.getByRole("link", LINK)).toHaveAttribute("href", localLinkUrl(8765, BASE, "acme", "alpha"));
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
  it("falls back to the status's sentence for an unknown code, never the server's text", () => {
    expect(linkError(new ApiError(500, "boom", "weird"))).toMatch(/WhyGraph hit an error/);
    expect(linkError(new ApiError(500, "boom", "constructor"))).toMatch(/WhyGraph hit an error/);
  });
  it("labels every revocation reason", () => {
    for (const r of ["user_revoked", "admin_revoked", "removed_locally", "member_removed", "member_left", "user_disabled", "project_deleted", "org_deleted", "idle", "project_access_removed"]) {
      expect(revokedLabel(r)).not.toBe("Revoked");
    }
    expect(revokedLabel(null)).toBe("Revoked");
  });
});
