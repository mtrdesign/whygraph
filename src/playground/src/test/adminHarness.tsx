import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { vi } from "vitest";
import { setAdminSessionSeen, setBaseUrl, type AdminInstanceSettings } from "../api";
import { hardNavigate } from "../lib/navigation";
import { createAppRouter } from "../router";
import { useUi } from "../store";
import { ThemeProvider } from "../theme";

// A fake-fetch harness for the admin app's tests (plan 59, §9.2). Like
// `identity.test.tsx`'s, but every path outside the admin app answers
// `401 login_required`, so a stray call to an everyday route (which would
// hard-navigate a password admin to `/signin`) shows up in `strays()`.

export const BASE = "http://whygraph.localhost:8765";
export const hard = vi.mocked(hardNavigate);

export type Handler = (method: string, body: unknown) => Response;

export const ME = {
  uid: "u1",
  display_name: "Ada",
  github_login: "ada",
  avatar_url: null,
  has_password: false,
  session: { created_at: "2026-11-18T08:00:00Z", expires_at: "2026-11-18T16:00:00Z" },
  reader_grants: [],
};

/** `GET /api/admin/config`: the defaults, a clean address check and an up-to-date instance. */
export const CONFIG = {
  settings: {
    signup_policy: "open",
    org_creation: "everyone",
    max_orgs_per_user: null,
    max_projects_per_org: null,
    banner_text: null,
    banner_level: "info",
    updated_at: null,
    updated_by: null,
  } as AdminInstanceSettings,
  instance: {
    base_url: BASE,
    base_check: [] as string[] | null,
    version: "2.6.0",
    update: {
      enabled: true,
      current: "2.6.0",
      latest: "2.6.0",
      available: false,
      checked_at: "2026-11-18T08:00:00Z",
      error: null,
      source: "default",
      notes_url: null,
      manual_steps: false,
      upgrading_url: "https://mtrdesign.github.io/whygraph/deploy/production/#upgrading",
    },
    github_app: { slug: "whygraph-app", client_id: "Iv1.app" },
    oauth_app: { client_id: "Iv1.oauth" },
    data_disk: { total: 107374182400, free: 53687091200 },
    postgres_bytes: 1288490189,
  },
};

export const fake: {
  state: Record<string, unknown>;
  calls: { path: string; method: string; body: unknown; search: string }[];
  routes: Record<string, Handler>;
} = { state: {}, calls: [], routes: {} };

export function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

export function baseState(over: Record<string, unknown> = {}) {
  return {
    mode: "production",
    host_kind: "base",
    base_url: BASE,
    setup_complete: true,
    bootstrap_required: false,
    user: null,
    org: null,
    ...over,
  };
}

const ADMIN_PATH = /^\/api\/(admin\/|auth\/admin\/|auth\/github\/admin\/|portal\/state$)/;

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const path = url.pathname;
  let body: unknown;
  try {
    body = init?.body ? JSON.parse(String(init.body)) : undefined;
  } catch {
    body = init?.body;
  }
  const method = init?.method ?? "GET";
  fake.calls.push({ path, method, body, search: url.search });
  if (path === "/api/portal/state") return Promise.resolve(json(fake.state));
  if (!ADMIN_PATH.test(path)) {
    return Promise.resolve(json({ error: "sign in", code: "login_required" }, 401));
  }
  const route = fake.routes[`${method} ${path}`] ?? fake.routes[path];
  if (route) return Promise.resolve(route(method, body));
  return Promise.resolve(json({ error: "unhandled" }, 500));
}

/** Calls to a path the admin app must never use. */
export function strays() {
  return fake.calls.filter((c) => !ADMIN_PATH.test(c.path));
}

export function resetAdminFake(over: Record<string, unknown> = {}) {
  fake.state = baseState(over);
  fake.calls = [];
  fake.routes = {};
  hard.mockClear();
  setBaseUrl(null);
  setAdminSessionSeen(false);
  window.localStorage.clear();
  useUi.setState({ paletteOpen: false, navOpen: false });
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })),
  );
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
}

export function mountAdmin(path: string) {
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

/** The common admin routes: a signed-in admin with a few users and organizations. */
export function signedInAdmin() {
  fake.routes["/api/admin/me"] = () => json(ME);
  fake.routes["/api/admin/users"] = () => json(USERS);
  fake.routes["/api/admin/orgs"] = () => json(ORGS);
  fake.routes["/api/admin/orgs/acme"] = () => json(ORG_DETAIL);
  fake.routes["/api/admin/users/u2"] = () => json(USER_DETAIL);
  fake.routes["/api/admin/config"] = () => json(CONFIG);
  fake.routes["/api/admin/audit"] = () => json({ events: [], next: null });
  fake.routes["/api/admin/projects"] = () => json({ projects: PROJECTS });
  fake.routes["/api/admin/projects/acme/api"] = () => json(PROJECT_DETAIL);
  fake.routes["/api/admin/stats/overview"] = () => json(STATS_OVERVIEW);
  fake.routes["/api/admin/stats/series"] = () => json({ from: "", to: "", columns: [], rows: [], stack: null });
  fake.routes["/api/admin/stats/top"] = () => json({ from: "", to: "", metric: "spend", items: [] });
}

/** The Phase 2 user fields of a live account. */
const GONE = { owner_of: 0, session_last_seen_at: null, deleted_at: null, deleted_by: null, erased: false };

export const USERS = [
  {
    uid: "u1",
    email: null,
    display_name: "Ada",
    is_instance_admin: true,
    github_login: "ada",
    has_password: false,
    disabled: false,
    created_at: "2026-10-01T00:00:00Z",
    org_count: 1,
    ...GONE,
    owner_of: 1,
  },
  {
    uid: "u2",
    email: null,
    display_name: "Ben",
    is_instance_admin: false,
    github_login: "ben",
    has_password: false,
    disabled: false,
    created_at: "2026-10-02T00:00:00Z",
    org_count: 2,
    ...GONE,
    session_last_seen_at: "2026-11-18T07:00:00Z",
  },
  {
    uid: "u3",
    email: "root@example.com",
    display_name: "Root",
    is_instance_admin: true,
    github_login: null,
    has_password: true,
    disabled: false,
    created_at: "2026-09-01T00:00:00Z",
    org_count: 0,
    ...GONE,
  },
];

export const ORGS = [
  {
    slug: "acme",
    name: "Acme",
    url: "http://acme.whygraph.localhost:8765",
    member_count: 3,
    created_at: "2026-10-01T00:00:00Z",
    project_count: 2,
    owners: ["ada"],
    suspended: null,
    deleted_at: null,
    deleted_by: null,
    spend_mtd: 1.5,
    storage_bytes: null,
    last_activity_day: "2026-11-17",
  },
];

export const ORG_DETAIL = {
  slug: "acme",
  name: "Acme",
  url: "http://acme.whygraph.localhost:8765",
  created_at: "2026-10-01T00:00:00Z",
  default_project_role: "contributor",
  suspended: null,
  deleted_at: null,
  deleted_by: null,
  members: [
    { uid: "u1", display_name: "Ada", github_login: "ada", role: "owner", joined_at: "2026-10-01T00:00:00Z", has_password: false, disabled: false },
    { uid: "u2", display_name: "Ben", github_login: "ben", role: "member", joined_at: "2026-10-02T00:00:00Z", has_password: false, disabled: false },
    { uid: "u3", display_name: "Root", github_login: null, role: "admin", joined_at: "2026-10-03T00:00:00Z", has_password: true, disabled: false },
  ],
  open_invitations: 1,
  projects: [
    { slug: "api", name: "Api", source: "github", last_scan_at: "2026-11-17T10:00:00Z", last_run_status: "ok", access_lost_reason: null, storage_bytes: null, deleted_at: null },
    { slug: "old", name: "Old", source: "github", last_scan_at: null, last_run_status: null, access_lost_reason: null, storage_bytes: null, deleted_at: "2026-11-10T00:00:00Z" },
  ],
  budgets: [{ scope: "org", monthly_usd: 50, hard_stop: true, spent_usd: 1.5, project: null, member: null }],
  spend_mtd: 1.5,
  rows: { usage_events: 12, audit_events: 40, agent_call_days: 3 },
};

export const USER_DETAIL = {
  ...USERS[1],
  memberships: [{ org: "acme", name: "Acme", role: "member", owner: false }],
  sessions: [{ id: "s1", kind: "user", created_at: "2026-11-18T06:00:00Z", last_seen_at: "2026-11-18T07:00:00Z", user_agent: "Firefox" }],
  tokens: [
    { uid: "t1", org: "acme", project: "api", project_name: "Api", client_name: "ben-laptop", created_at: "2026-11-01T00:00:00Z", last_used_at: null, revoked_at: null, revoked_reason: null },
  ],
  spend: [{ org: "acme", spent_usd: 0.5 }],
  events: [],
};

export const PROJECTS = [
  {
    org: "acme",
    org_deleted_at: null,
    slug: "api",
    name: "Api",
    source: "github",
    created_at: "2026-10-05T00:00:00Z",
    last_scan_at: "2026-11-17T10:00:00Z",
    last_run: { status: "ok", finished_at: "2026-11-17T10:05:00Z" },
    access_lost_reason: null,
    restricted: false,
    deleted_at: null,
    storage_bytes: 1288490189,
    spend_mtd: 0.75,
  },
  {
    org: "acme",
    org_deleted_at: null,
    slug: "old",
    name: "Old",
    source: "github",
    created_at: "2026-10-06T00:00:00Z",
    last_scan_at: null,
    last_run: null,
    access_lost_reason: null,
    restricted: false,
    deleted_at: "2026-11-10T00:00:00Z",
    storage_bytes: null,
    spend_mtd: 0,
  },
  {
    org: "zeta",
    org_deleted_at: null,
    slug: "web",
    name: "Web",
    source: "github",
    created_at: "2026-10-07T00:00:00Z",
    last_scan_at: "2026-11-16T10:00:00Z",
    last_run: { status: "failed", finished_at: "2026-11-16T10:05:00Z" },
    access_lost_reason: null,
    restricted: false,
    deleted_at: null,
    storage_bytes: 524288,
    spend_mtd: 0,
  },
];

export const PROJECT_DETAIL = {
  org: "acme",
  org_deleted_at: null,
  org_suspended: null,
  slug: "api",
  name: "Api",
  source: "github",
  created_at: "2026-10-05T00:00:00Z",
  last_scan_at: "2026-11-17T10:00:00Z",
  access_lost_reason: null,
  restricted: false,
  deleted_at: null,
  deleted_by: null,
  overview: {
    coverage: { points: [] },
    events: [],
    last_failure: null,
    usage: { month: "2026-11", spent_usd: 0.75, budget_usd: 5, pct: 15, by_task: [] },
    agents: { days: [], total_calls: 42, llm_calls: 0, llm_cost_usd: 0, errors: 0, internal_errors: 0, by_kind: [], people: null, connections: null, last_call_day: null, chat_tools: { total_calls: 0, errors: 0, internal_errors: 0, by_kind: [] } },
  },
  runs: [
    { id: 7, kind: "sync", trigger: "manual", analyze: false, status: "ok", queued_at: "2026-11-17T09:59:00Z", started_at: "2026-11-17T10:00:00Z", finished_at: "2026-11-17T10:05:00Z", requested_by: { uid: "u2", label: "Ben" }, summary: null },
  ],
  storage: {
    latest: { day: "2026-11-18", measured_at: "2026-11-18T03:00:00Z", history_bytes: 1048576, git_bytes: 2097152, codegraph_bytes: 524288, worktree_bytes: 4194304, runs_bytes: 1024, total_bytes: 7864320 },
    series: { from: "2026-08-21", to: "2026-11-18", columns: ["day", "bytes"], rows: [["2026-11-17", 7000000], ["2026-11-18", 7864320]] },
  },
};

export const STATS_OVERVIEW = {
  from: "2026-10-20",
  to: "2026-11-19",
  kpis: {
    users: { value: 3, previous: 2 },
    orgs: { value: 1, previous: 1 },
    projects: { value: 2, previous: null },
    llm_spend_mtd: { value: 12.5, previous: 8 },
    tokens_mtd: { value: 1234567, previous: null },
    storage_bytes: { value: 1288490189, previous: 1073741824 },
    agent_calls_7d: { value: 150, previous: 100 },
  },
  attention: [] as Record<string, unknown>[],
};
