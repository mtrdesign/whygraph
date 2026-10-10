import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProjectSummary } from "../api";
import { setBaseUrl } from "../api";
import { dismissKey } from "../components/onboarding/firstRun";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

// M2f-3 S20: the first-run checklist (local 3 / production 5), "Connect your
// agent", the welcome banner, and the org pages' first-visit copy.

const BASE = "http://whygraph.localhost:8765";
type Json = Record<string, unknown>;
type Item = { id: string; done: boolean; can_act: boolean };

interface Fake {
  state: Json;
  projects: ProjectSummary[];
  items: Item[];
  slug: { status: number; body: unknown };
  orgs: unknown[];
  dismissWelcome: number;
  log: { method: string; path: string }[];
}
let fake: Fake;

const LOCAL: Json = { mode: "local", setup_complete: true, user: { uid: "u1", display_name: "Ada", role: "owner" } };
const ben = { uid: "u2", display_name: "Ben", email: null, github_login: "ben", role: null };
const prod = (role: string, over: Json = {}): Json => ({
  mode: "production",
  host_kind: "org",
  base_url: BASE,
  setup_complete: true,
  user: ben,
  org: { slug: "acme", name: "Acme", role },
  ...over,
});

function project(slug: string, over: Partial<ProjectSummary> = {}): ProjectSummary {
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
    my_role: "admin",
    permissions: PROJECT_ACTIONS,
    last_scan_status: "ok",
    stale: null,
    source_supported: true,
    access_lost: false,
    access_lost_reason: null,
    github_full_name: null,
    installation_account: null,
    ...over,
  } as ProjectSummary;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const method = init?.method ?? "GET";
  fake.log.push({ method, path: url.pathname + url.search });
  switch (url.pathname) {
    case "/api/portal/state":
      return Promise.resolve(json(fake.state));
    case "/api/projects":
      return Promise.resolve(json({ projects: fake.projects }));
    case "/api/onboarding":
      return Promise.resolve(json({ items: fake.items }));
    case "/api/org/welcome":
      fake.dismissWelcome += 1;
      return Promise.resolve(fake.dismissWelcome === 1 && failWelcome ? json({ error: "x" }, 500) : new Response(null, { status: 204 }));
    case "/api/orgs/slug-check":
      return Promise.resolve(json(fake.slug.body, fake.slug.status));
    case "/api/account/orgs":
      return Promise.resolve(json(fake.orgs));
  }
  return Promise.resolve(json({ error: "unhandled" }, 500));
}
let failWelcome = false;

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
  failWelcome = false;
  fake = {
    state: LOCAL,
    projects: [],
    items: [],
    slug: { status: 200, body: { slug: "acme", available: true, reason: null } },
    orgs: [],
    dismissWelcome: 0,
    log: [],
  };
  setBaseUrl(null);
  window.localStorage.clear();
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

describe("FirstRunChecklist - local", () => {
  beforeEach(() => {
    fake.items = [
      { id: "llm_key", done: false, can_act: true },
      { id: "project", done: false, can_act: true },
      { id: "agent", done: false, can_act: true },
    ];
  });

  it("is the empty state: three numbered items with their actions", async () => {
    mount("/");
    const list = await screen.findByTestId("first-run-list");
    expect(screen.getByRole("heading", { name: "No projects yet" })).toBeInTheDocument();
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(screen.getByTestId("first-run-llm_key")).getByRole("link", { name: "Add a key" })).toHaveAttribute(
      "href",
      "/settings?section=models",
    );
    expect(within(screen.getByTestId("first-run-project")).getByRole("link", { name: "Add project" })).toHaveAttribute(
      "href",
      "/projects/new",
    );
    // No project yet: the agent step leads to adding one.
    expect(within(screen.getByTestId("first-run-agent")).getByRole("link")).toHaveAttribute("href", "/projects/new");
  });

  it("sends the agent step to the first project's Overview", async () => {
    fake.projects = [project("alpha")];
    fake.items[1].done = true;
    mount("/");
    const agent = await screen.findByTestId("first-run-agent");
    expect(within(agent).getByRole("link", { name: "Open the Overview" })).toHaveAttribute("href", "/p/alpha");
    expect(screen.getByRole("heading", { name: "Getting started" })).toBeInTheDocument();
    expect(within(screen.getByTestId("first-run-project")).queryByRole("link")).toBeNull();
  });

  it("is dismissed per browser, and the header link brings it back", async () => {
    const user = userEvent.setup();
    fake.projects = [project("alpha")];
    mount("/");
    await screen.findByTestId("first-run-checklist");
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("first-run-checklist")).toBeNull();
    expect(window.localStorage.getItem(dismissKey("local", "u1"))).toBe("1");
    await user.click(screen.getByTestId("getting-started-link"));
    expect(await screen.findByTestId("first-run-checklist")).toBeInTheDocument();
    expect(window.localStorage.getItem(dismissKey("local", "u1"))).toBeNull();
  });

  it("falls back to the plain empty state once dismissed", async () => {
    window.localStorage.setItem(dismissKey("local", "u1"), "1");
    mount("/");
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    expect(screen.queryByTestId("first-run-checklist")).toBeNull();
    expect(screen.getByRole("link", { name: "Add project" })).toHaveAttribute("href", "/projects/new");
  });

  it("is hidden when every item is done", async () => {
    fake.projects = [project("alpha")];
    fake.items = fake.items.map((i) => ({ ...i, done: true }));
    mount("/");
    await screen.findByTestId("project-alpha");
    await waitFor(() => expect(fake.log.some((c) => c.path === "/api/onboarding")).toBe(true));
    expect(screen.queryByTestId("first-run-checklist")).toBeNull();
    expect(screen.queryByTestId("getting-started-link")).toBeNull();
  });

  it("notes the platform's keys when the LLM key item is omitted", async () => {
    fake.projects = [project("alpha", { source: "platform" })];
    fake.items = [
      { id: "project", done: true, can_act: true },
      { id: "agent", done: false, can_act: true },
    ];
    mount("/");
    expect(await screen.findByTestId("first-run-linked-note")).toHaveTextContent("Linked projects use the platform's keys.");
  });
});

describe("FirstRunChecklist - production", () => {
  beforeEach(() => {
    fake.state = prod("owner");
    fake.items = [
      { id: "github", done: false, can_act: true },
      { id: "project", done: false, can_act: true },
      { id: "llm_key", done: false, can_act: true },
      { id: "invite", done: false, can_act: true },
      { id: "agent", done: false, can_act: true },
    ];
  });

  it("shows five items in the server's order with the right actions", async () => {
    mount("/");
    const list = await screen.findByTestId("first-run-list");
    expect(within(list).getAllByRole("listitem").map((li) => li.getAttribute("data-testid"))).toEqual([
      "first-run-github",
      "first-run-project",
      "first-run-llm_key",
      "first-run-invite",
      "first-run-agent",
    ]);
    expect(screen.getByRole("link", { name: "Install the WhyGraph GitHub App" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import a repository" })).toHaveAttribute("href", "/projects/new");
    expect(screen.getByRole("link", { name: "Invite" })).toHaveAttribute("href", "/members");
  });

  it("asks a password account (the bootstrap owner) to sign in with GitHub", async () => {
    fake.state = prod("owner", { user: { ...ben, github_login: null } });
    fake.items[0].can_act = false;
    mount("/");
    const github = await screen.findByTestId("first-run-github");
    expect(github).toHaveTextContent("Sign in with GitHub to import repositories");
    expect(within(github).queryByRole("link")).toBeNull();
  });

  it("says 'Ask an owner' to an admin who cannot add the key", async () => {
    fake.state = prod("admin");
    fake.items[2].can_act = false;
    mount("/");
    const key = await screen.findByTestId("first-run-llm_key");
    expect(key).toHaveTextContent("Ask an owner");
    expect(within(key).queryByRole("link")).toBeNull();
  });

  it("does not ask for the items without org.add_project", async () => {
    fake.state = prod("member");
    mount("/");
    await screen.findByText("No projects shared with you yet");
    expect(fake.log.some((c) => c.path === "/api/onboarding")).toBe(false);
  });

  it("opens Connect your agent, with the hint when there is no project", async () => {
    const user = userEvent.setup();
    mount("/");
    await user.click(await screen.findByRole("button", { name: "Connect your agent" }));
    const dialog = await screen.findByTestId("connect-agent-dialog");
    expect(within(dialog).getByText("whygraph up")).toBeInTheDocument();
    expect(await within(dialog).findByTestId("connect-agent-no-projects")).toHaveTextContent(
      "Open a project, then Use with your agent.",
    );
    expect(within(dialog).queryByTestId("use-with-agent")).toBeNull();
  });

  it("picks a project for the per-project link", async () => {
    const user = userEvent.setup();
    fake.projects = [project("alpha", { source: "github" }), project("beta", { source: "github" })];
    mount("/");
    await user.click(await screen.findByRole("button", { name: "Connect your agent" }));
    const dialog = await screen.findByTestId("connect-agent-dialog");
    expect(within(dialog).queryByTestId("use-with-agent")).toBeNull();
    await user.selectOptions(await within(dialog).findByLabelText("Project"), "beta");
    expect(await within(dialog).findByTestId("use-with-agent")).toBeInTheDocument();
  });

  it("uses the only project without asking", async () => {
    const user = userEvent.setup();
    fake.projects = [project("alpha", { source: "github" })];
    mount("/");
    await user.click(await screen.findByRole("button", { name: "Connect your agent" }));
    const dialog = await screen.findByTestId("connect-agent-dialog");
    expect(await within(dialog).findByTestId("use-with-agent")).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Project")).toBeNull();
  });
});

describe("WelcomeBanner", () => {
  beforeEach(() => {
    fake.state = prod("member", { welcome: { org_name: "Acme", role: "member" } });
    fake.projects = [project("alpha", { source: "github" })];
  });

  it("welcomes the new member and dismisses optimistically", async () => {
    const user = userEvent.setup();
    mount("/");
    const banner = await screen.findByTestId("welcome-banner");
    expect(banner).toHaveTextContent("You've been added to Acme as a Member.");
    expect(banner).toHaveTextContent("Next: connect your agent");
    await user.click(within(banner).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("welcome-banner")).toBeNull();
    await waitFor(() => expect(fake.dismissWelcome).toBe(1));
  });

  it("uses 'an' for an admin and opens the dialog", async () => {
    const user = userEvent.setup();
    fake.state = prod("admin", { welcome: { org_name: "Acme", role: "admin" } });
    mount("/");
    const banner = await screen.findByTestId("welcome-banner");
    expect(banner).toHaveTextContent("as an Admin.");
    await user.click(within(banner).getByRole("button", { name: "Connect your agent" }));
    expect(await screen.findByTestId("connect-agent-dialog")).toBeInTheDocument();
  });

  it("retries a failed dismissal on the next load without showing the banner again", async () => {
    const user = userEvent.setup();
    failWelcome = true;
    const { unmount } = (() => {
      mount("/");
      return { unmount: () => document.body.replaceChildren() };
    })();
    await user.click(await screen.findByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(fake.dismissWelcome).toBe(1));
    unmount();
    // The server still has the flag; a fresh load hides the banner and asks again.
    mount("/");
    await waitFor(() => expect(fake.dismissWelcome).toBe(2));
    await screen.findByTestId("projects-count");
    expect(screen.queryByTestId("welcome-banner")).toBeNull();
  });

  it("is absent without the flag", async () => {
    fake.state = prod("member");
    mount("/");
    await screen.findByTestId("projects-count");
    expect(screen.queryByTestId("welcome-banner")).toBeNull();
  });
});

describe("org pages", () => {
  const baseState = (over: Json = {}): Json => ({
    mode: "production",
    host_kind: "base",
    base_url: BASE,
    setup_complete: true,
    bootstrap_required: false,
    user: ben,
    org: null,
    ...over,
  });

  it("CreateOrgPage hints at joining a team to a first-time GitHub user", async () => {
    fake.state = baseState();
    mount("/orgs/new");
    expect(await screen.findByTestId("join-hint")).toHaveTextContent(
      "Joining a team? You don't need your own organization. Ask an owner to add your GitHub username @ben, then sign in again.",
    );
  });

  it("CreateOrgPage has no hint for someone who already has an organization", async () => {
    fake.state = baseState();
    fake.orgs = [{ slug: "acme", name: "Acme", role: "owner", url: "http://acme.whygraph.localhost:8765" }];
    mount("/orgs/new");
    await screen.findByLabelText("Organization name");
    await waitFor(() => expect(fake.log.some((c) => c.path === "/api/account/orgs")).toBe(true));
    expect(screen.queryByTestId("join-hint")).toBeNull();
  });

  it("checks the slug as you type (debounced) and says what it found", async () => {
    const user = userEvent.setup();
    fake.state = baseState();
    mount("/orgs/new");
    await user.type(await screen.findByLabelText("Organization name"), "Acme");
    expect(await screen.findByTestId("slug-status", {}, { timeout: 3000 })).toHaveTextContent(
      "acme.whygraph.localhost:8765 is available",
    );
    // One request for the settled slug, not one per keystroke.
    expect(fake.log.filter((c) => c.path.startsWith("/api/orgs/slug-check"))).toHaveLength(1);

    fake.slug = { status: 200, body: { slug: "admin", available: false, reason: "reserved" } };
    const slug = screen.getByLabelText("URL name");
    await user.clear(slug);
    await user.type(slug, "admin");
    await waitFor(() => expect(screen.getByTestId("slug-status")).toHaveTextContent("admin.whygraph.localhost:8765 is reserved"), {
      timeout: 3000,
    });
    fake.slug = { status: 200, body: { slug: "taken", available: false, reason: "taken" } };
    await user.clear(slug);
    await user.type(slug, "taken");
    await waitFor(() => expect(screen.getByTestId("slug-status")).toHaveTextContent("is already taken"), { timeout: 3000 });
  });

  it("says nothing when the check is throttled", async () => {
    const user = userEvent.setup();
    fake.state = baseState();
    fake.slug = { status: 429, body: { error: "slow down", code: "throttled" } };
    mount("/orgs/new");
    await user.type(await screen.findByLabelText("Organization name"), "Acme");
    await waitFor(() => expect(fake.log.some((c) => c.path.startsWith("/api/orgs/slug-check"))).toBe(true), { timeout: 3000 });
    expect(screen.queryByTestId("slug-status")).toBeNull();
  });

  it("NoOrgAccessPage names the org and carries the hint", async () => {
    fake.state = prod("none", { org: null });
    // The org is named by the address; jsdom's host is the test's own.
    mount("/");
    const hint = await screen.findByTestId("join-hint");
    expect(hint).toHaveTextContent("Ask an owner to add your GitHub username @ben, then sign in again.");
    expect(screen.getByText(/is not a member of/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Your organizations" })).toHaveAttribute("href", `${BASE}/orgs`);
  });

  it("the bootstrap page gives the exact log command", async () => {
    fake.state = baseState({ bootstrap_required: true, user: null });
    mount("/signin");
    expect(await screen.findByTestId("bootstrap-command")).toHaveTextContent(
      'docker compose logs portal | grep "Bootstrap secret:"',
    );
  });
});
