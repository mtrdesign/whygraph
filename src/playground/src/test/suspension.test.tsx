import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setBaseUrl, type ProjectSummary } from "../api";
import { scanAvailability } from "../lib/scanAvailability";
import { canAdmin, canOwn, useRole, useSuspended } from "../lib/identity";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { createAppRouter } from "../router";
import { useUi } from "../store";
import { ThemeProvider } from "../theme";

// Plan 59, §4.5: an owner of a suspended organization sees the banner and its reason, the pages
// stay readable, every write control is gone, and no sentence says "instance administrator".

const BASE = "http://whygraph.localhost:8765";
const SUSPENSION = { at: "2026-11-01T09:00:00Z", reason: "Unpaid invoice" };

let state: Record<string, unknown>;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const orgState = (role: string, suspended: typeof SUSPENSION | null) => ({
  mode: "production",
  host_kind: "org",
  base_url: BASE,
  setup_complete: true,
  bootstrap_required: false,
  user: { uid: "u1", display_name: "Ada", email: "ada@example.com", role: null, is_instance_admin: false },
  org: { slug: "acme", name: "Acme", role, suspended },
});

// What the server answers for a suspended org: the write permissions are dropped.
const READ = ["project.read", "project.usage", "project.access"];
const project = (over: Record<string, unknown> = {}) => ({
  slug: "alpha",
  name: "Project alpha",
  source: "github",
  root: "/r/alpha",
  remote_url: null,
  initialized: true,
  initialized_at: "2026-01-01T00:00:00Z",
  last_scan_at: "2026-01-01T00:00:00Z",
  created_at: "2026-01-01T00:00:00Z",
  root_status: "ok",
  running_scan: null,
  restricted: false,
  my_role: "admin",
  permissions: READ,
  stale: null,
  agents: [],
  missing_key: null,
  mcp_url: null,
  stats: null,
  llm_block: "org_suspended",
  ...over,
});

function fakeFetch(input: RequestInfo | URL): Promise<Response> {
  const path = new URL(String(input), "http://127.0.0.1:8765").pathname;
  if (path === "/api/portal/state") return Promise.resolve(json(state));
  if (path === "/api/projects") return Promise.resolve(json({ projects: [project()] }));
  if (path === "/api/projects/alpha") return Promise.resolve(json(project()));
  if (path === "/api/projects/alpha/scans") return Promise.resolve(json({ runs: [] }));
  if (path === "/api/projects/alpha/chat/sessions") return Promise.resolve(json([]));
  if (path === "/api/org/members") return Promise.resolve(json([]));
  if (path === "/api/projects/alpha/access") {
    return Promise.resolve(
      json({
        restricted: false,
        org_default: "viewer",
        people: [{ uid: "u2", login: "ben", name: "Ben", avatar: null, org_role: "member", project_role: "viewer", source: "grant" }],
        invitations: [],
      }),
    );
  }
  if (path === "/api/portal/defaults") {
    return Promise.resolve(json({ config: {}, secrets: {}, read_only: true, can_test_keys: false, effective_keys: {} }));
  }
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

beforeEach(() => {
  state = orgState("owner", SUSPENSION);
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

describe("an owner of a suspended organization", () => {
  it("sees the banner with its reason, and no Rescan on the project", async () => {
    mount("/p/alpha");
    const banner = await screen.findByTestId("suspended-banner");
    expect(banner).toHaveTextContent("This organization is suspended: Unpaid invoice. Data is read-only.");
    await screen.findByRole("heading", { name: "Project alpha" });
    expect(screen.queryByRole("button", { name: "Rescan" })).toBeNull();
    expect(document.body).not.toHaveTextContent(/instance admin/i);
  });

  it("has no banner when the organization is not suspended", async () => {
    state = orgState("owner", null);
    mount("/p/alpha");
    await screen.findByRole("heading", { name: "Project alpha" });
    expect(screen.queryByTestId("suspended-banner")).toBeNull();
  });

  it("has no Import from GitHub on the Projects page", async () => {
    mount("/");
    await screen.findByTestId("suspended-banner");
    await screen.findByText("Project alpha");
    expect(screen.queryByRole("link", { name: /Import from GitHub/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Import from GitHub/ })).toBeNull();
  });

  it("has no Invite someone form on Members, and no Leave organization", async () => {
    mount("/members");
    await screen.findByTestId("members-heading");
    expect(screen.queryByText("Invite someone")).toBeNull();
    expect(screen.queryByTestId("add-member")).toBeNull();
    expect(screen.queryByRole("button", { name: "Leave organization" })).toBeNull();
  });

  it("is told Chat is paused on the Chat route, never that it needs the Contributor role", async () => {
    mount("/p/alpha/chat");
    const notice = await screen.findByTestId("chat-read-only");
    expect(notice).toHaveTextContent("Chat is paused");
    expect(notice).toHaveTextContent(
      "This organization is suspended. Chat comes back when a platform administrator lifts the suspension.",
    );
    expect(notice).not.toHaveTextContent("Contributor");
  });

  it("shows no Chats section at all: the server drops the chat permission", async () => {
    mount("/p/alpha");
    await screen.findByTestId("suspended-banner");
    await screen.findByRole("heading", { name: "Project alpha" });
    expect(screen.queryByTestId("chats-section")).toBeNull();
  });

  it("sees the suspension notice, not the owner-only one, on the organization settings", async () => {
    mount("/settings");
    const notice = await screen.findByTestId("settings-suspended");
    expect(notice).toHaveTextContent(
      "This organization is suspended, so everything here is read-only. A platform administrator can lift the suspension.",
    );
    expect(screen.queryByTestId("org-danger-zone")).toBeNull();
    expect(document.body).not.toHaveTextContent(/instance admin/i);
  });

  it("sees the suspension notice on the project settings, with no Danger zone", async () => {
    mount("/p/alpha/settings");
    expect(await screen.findByTestId("settings-suspended")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove project" })).toBeNull();
  });

  it("keeps the project's Access section readable but with its edit controls off", async () => {
    mount("/p/alpha/settings?section=access");
    const access = await screen.findByTestId("project-access");
    expect(await within(access).findByRole("switch", { name: /Restricted/ })).toHaveAttribute("data-disabled");
    expect(within(access).getByLabelText("Role for Ben")).toBeDisabled();
    expect(within(access).getByRole("button", { name: "Remove" })).toBeDisabled();
  });

  it("reads as a reader to every role-gated write control (budget edit, Invite, Add project)", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function Probe() {
      const role = useRole();
      return (
        <p data-testid="probe">
          {String(role)} admin={String(canAdmin(role))} owner={String(canOwn(role))} suspended={String(useSuspended())}
        </p>
      );
    }
    render(
      <QueryClientProvider client={queryClient}>
        <Probe />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("reader admin=false owner=false suspended=true"));
  });
});

describe("llm_block: org_suspended", () => {
  const summary = (over: Partial<ProjectSummary>) =>
    ({ slug: "a", name: "A", source: "github", initialized: true, root_status: "ok", permissions: PROJECT_ACTIONS, ...over }) as ProjectSummary;

  it("is read by scanAvailability before the role: both scans say the organization is suspended", () => {
    const a = scanAvailability(summary({ llm_block: "org_suspended", permissions: READ as ProjectSummary["permissions"] }));
    expect(a.quick).toEqual({ allowed: false, reason: "The organization is suspended" });
    expect(a.full).toEqual({ allowed: false, reason: "The organization is suspended" });
  });

  it("names it even for an admin whose permissions were not trimmed", () => {
    const a = scanAvailability(summary({ llm_block: "org_suspended" }));
    expect(a.quick).toEqual({ allowed: false, reason: "The organization is suspended" });
    expect(a.full.allowed).toBe(false);
  });
});
