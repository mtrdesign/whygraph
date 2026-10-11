import { act, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRouter } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { EstimateBody } from "../components/portal/ScanEstimateCard";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

// M2f-3 S12's correctness fixes that have no older test file of their own: the
// members page's selects (BUG-1) and own row (BUG-25), and the estimate's
// pluralisation (BUG-15).

const ORG = "http://acme.whygraph.localhost:8765";
const BASE = "http://whygraph.localhost:8765";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function orgState(role: string) {
  return {
    mode: "production",
    host_kind: "org",
    base_url: BASE,
    setup_complete: true,
    bootstrap_required: false,
    user: { uid: "u1", display_name: "Ada", email: null, role: null, is_instance_admin: false, github_login: "ada" },
    org: { slug: "acme", name: "Acme", role },
  };
}

const member = (uid: string, name: string, role: string) => ({
  uid,
  display_name: name,
  github_login: name.toLowerCase(),
  avatar_url: null,
  role,
  joined_at: "2026-10-01T00:00:00Z",
  disabled: false,
});

const project = (slug: string) => ({
  slug,
  name: `Project ${slug}`,
  source: "github",
  root: null,
  remote_url: null,
  initialized: true,
  initialized_at: "2026-10-01T00:00:00Z",
  last_scan_at: null,
  created_at: "2026-10-01T00:00:00Z",
  root_status: "ok",
  running_scan: null,
  restricted: false,
  my_role: "admin",
  permissions: PROJECT_ACTIONS,
  stale: null,
});

let state: Record<string, unknown>;

function fakeFetch(input: RequestInfo | URL): Promise<Response> {
  const path = new URL(String(input), ORG).pathname;
  if (path === "/api/portal/state") return Promise.resolve(json(state));
  if (path === "/api/projects") return Promise.resolve(json({ projects: [project("alpha")] }));
  if (path === "/api/org/members") {
    return Promise.resolve(json([member("u1", "Ada", "owner"), member("u2", "Olga", "owner"), member("u4", "Meg", "member")]));
  }
  if (path === "/api/org/invitations") return Promise.resolve(json([]));
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
}

beforeEach(() => {
  state = orgState("owner");
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

describe("Members page (BUG-1, BUG-25)", () => {
  it("sizes the invite form's project selects so each project's name shows", async () => {
    mount("/members");
    const grants = await screen.findByTestId("invite-grants");
    expect(within(grants).getByText("Project alpha")).toBeInTheDocument();
    const select = within(grants).getByLabelText("Access to Project alpha");
    expect(select.className).toContain("w-48");
    expect(select.className).not.toContain("w-full");
  });

  it("shows your own role as text, with why it cannot be changed here", async () => {
    mount("/members");
    const me = within(await screen.findByTestId("member-u1"));
    expect(me.queryByRole("combobox")).toBeNull();
    expect(me.getByTestId("own-role")).toHaveTextContent("Owner");
    expect(me.getByTestId("own-role-note")).toHaveTextContent("You can't change your own role");
    // Everyone else keeps the select, sized by the caller's width.
    const meg = within(screen.getByTestId("member-u4"));
    const select = meg.getByRole("combobox", { name: "Role for Meg" });
    expect(select.className).toContain("w-28");
    expect(select.className).not.toContain("w-full");
    expect(meg.queryByTestId("own-role-note")).toBeNull();
  });

  it("a plain member's own row has no note: they cannot change any role", async () => {
    state = orgState("member");
    mount("/members");
    const me = within(await screen.findByTestId("member-u1"));
    expect(me.queryByTestId("own-role-note")).toBeNull();
  });
});

describe("ScanEstimateCard pluralisation (BUG-15)", () => {
  async function renderBody(commits: number) {
    const root = createRootRoute({
      component: () => (
        <EstimateBody
          slug="a"
          estimate={{
            commits,
            upper_bound: true,
            large_commits: 0,
            model: { provider: "anthropic", model: "claude-haiku-4-5" },
            tokens: null,
            cost: null,
            missing_key: null,
          }}
          canDescribe
          canConfigure
          onDescribe={() => {}}
          onLater={() => {}}
        />
      ),
    });
    const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ["/"] }) });
    await act(async () => {
      render(<RouterProvider router={router} />);
    });
  }

  it("says 1 commit, not 1 commits", async () => {
    await renderBody(1);
    expect(screen.getByTestId("scan-estimate")).toHaveTextContent("1 commit to describe");
  });

  it("groups and pluralises larger counts", async () => {
    await renderBody(1204);
    expect(screen.getByTestId("scan-estimate")).toHaveTextContent("1,204 commits to describe");
  });
});
