import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BannerStrip } from "../components/shell/BudgetBanner";
import { dismissUpdate, errorLabel, isUpdateDismissed, sourceLabel } from "../lib/updates";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const block = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  source: "setting",
  current: "2.2.1",
  latest: "2.3.0",
  available: true,
  manual_steps: false,
  notes_url: "https://mtrdesign.github.io/whygraph/releases/2.3.0/",
  upgrading_url: "https://mtrdesign.github.io/whygraph/portal/upgrading/",
  checked_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  error: null,
  ...over,
});

const localState = (update: unknown) => ({
  mode: "local",
  setup_complete: true,
  user: { uid: "u1", display_name: "Ada", role: "owner" },
  port: 8765,
  shared_folders: ["/repos"],
  version: "2.2.1",
  update,
});

let state: Record<string, unknown>;
let routes: Record<string, (body: unknown) => Response>;
let calls: { key: string; body: unknown }[];

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = new URL(String(input), "http://127.0.0.1:8765").pathname;
  const key = `${init?.method ?? "GET"} ${path}`;
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  calls.push({ key, body });
  if (routes[key]) return Promise.resolve(routes[key](body));
  if (path === "/api/portal/state") return Promise.resolve(json(state));
  if (path === "/api/projects") return Promise.resolve(json({ projects: [] }));
  if (path === "/api/portal/defaults") return Promise.resolve(json({ config: {}, secrets: { llm: {}, github_token: { set: false, hint: null } }, no_provider_key: false }));
  return Promise.resolve(json({ error: `unhandled ${key}` }, 404));
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
  state = localState(block());
  routes = {};
  calls = [];
  useUi.setState({ paletteOpen: false, navOpen: false });
  window.localStorage.clear();
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })),
  );
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("update labels and storage", () => {
  it("words the source and the error", () => {
    expect(sourceLabel("do_not_track")).toBe("Off: DO_NOT_TRACK is set");
    expect(sourceLabel("env")).toBe("Set by WHYGRAPH_UPDATE_CHECK");
    expect(sourceLabel("setting")).toBe("");
    expect(errorLabel("network")).toBe("could not reach GitHub");
    expect(errorLabel("rate_limited")).toBe("GitHub rate limit, will retry");
    expect(errorLabel("bad_response")).toBe("unexpected answer from GitHub");
  });

  it("remembers a dismissal per version", () => {
    expect(isUpdateDismissed("2.3.0")).toBe(false);
    dismissUpdate("2.3.0");
    expect(window.localStorage.getItem("whygraph:update-banner:2.3.0")).toBe("1");
    expect(isUpdateDismissed("2.3.0")).toBe(true);
    expect(isUpdateDismissed("2.4.0")).toBe(false);
  });
});

describe("BannerStrip", () => {
  it("keeps its default dismiss label", () => {
    render(
      <BannerStrip tone="info" testId="x" onDismiss={() => {}}>
        hi
      </BannerStrip>,
    );
    expect(screen.getByRole("button", { name: "Dismiss for this month" })).toBeInTheDocument();
  });
});

describe("UpdateBanner", () => {
  it("is hidden when no update is available", async () => {
    state = localState(block({ available: false }));
    mount("/");
    await screen.findByRole("heading", { name: "Projects" });
    expect(screen.queryByTestId("update-banner")).toBeNull();
  });

  it("offers the command and dismisses per version", async () => {
    const user = userEvent.setup();
    mount("/");
    const banner = await screen.findByTestId("update-banner");
    expect(banner).toHaveTextContent("WhyGraph 2.3.0 is available (you have 2.2.1).");
    expect(banner).toHaveTextContent("whygraph update");
    expect(within(banner).getByRole("link", { name: "What's new" })).toHaveAttribute("target", "_blank");
    await user.click(within(banner).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("update-banner")).toBeNull();
    expect(window.localStorage.getItem("whygraph:update-banner:2.3.0")).toBe("1");
  });

  it("stays hidden once dismissed, and shows again for a newer version", async () => {
    window.localStorage.setItem("whygraph:update-banner:2.3.0", "1");
    mount("/");
    await screen.findByRole("heading", { name: "Projects" });
    expect(screen.queryByTestId("update-banner")).toBeNull();
    cleanup();
    state = localState(block({ latest: "2.4.0" }));
    mount("/");
    expect(await screen.findByTestId("update-banner")).toHaveTextContent("WhyGraph 2.4.0");
  });

  it("the manual variant links the upgrade guide and has no command", async () => {
    state = localState(block({ manual_steps: true }));
    mount("/");
    const banner = await screen.findByTestId("update-banner");
    expect(banner).toHaveTextContent("WhyGraph 2.3.0 is available and needs manual steps.");
    expect(banner).not.toHaveTextContent("whygraph update");
    expect(within(banner).getByRole("link", { name: "How to upgrade" })).toHaveAttribute(
      "href",
      "https://mtrdesign.github.io/whygraph/portal/upgrading/",
    );
    expect(within(banner).getByRole("link", { name: "What's new" })).toBeInTheDocument();
  });
});

describe("Settings updates row", () => {
  it("shows the switch on, the last check, and turns it off", async () => {
    routes["PUT /api/portal/update-check"] = () => json(block({ enabled: false }));
    const user = userEvent.setup();
    mount("/settings");
    const row = await screen.findByTestId("update-check-row");
    const sw = within(row).getByRole("switch");
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(row).toHaveTextContent("Last checked 5 min ago");
    await user.click(sw);
    await waitFor(() =>
      expect(calls.find((c) => c.key === "PUT /api/portal/update-check")?.body).toEqual({ enabled: false }),
    );
  });

  it("is disabled with the reason when the environment decides", async () => {
    state = localState(block({ enabled: false, source: "do_not_track", latest: null, available: false }));
    mount("/settings");
    const row = await screen.findByTestId("update-check-row");
    expect(within(row).getByRole("switch")).toHaveAttribute("aria-disabled", "true");
    expect(row).toHaveTextContent("Off: DO_NOT_TRACK is set");
  });

  it("words a failed check", async () => {
    state = localState(block({ error: "rate_limited" }));
    mount("/settings");
    expect(await screen.findByTestId("update-check-row")).toHaveTextContent(
      "Last check failed: GitHub rate limit, will retry",
    );
  });

  it("has no row without an update block", async () => {
    state = localState(null);
    mount("/settings");
    await screen.findByTestId("portal-general");
    expect(screen.queryByTestId("update-check-row")).toBeNull();
  });
});

describe("Admin updates card", () => {
  async function mountAdmin(over: Record<string, unknown>) {
    state = {
      mode: "production",
      host_kind: "base",
      base_url: "http://whygraph.localhost:8765",
      setup_complete: true,
      bootstrap_required: false,
      user: { uid: "u1", display_name: "Ada", email: "a@example.com", role: null, is_instance_admin: true },
      org: null,
    };
    routes["GET /api/admin/update"] = () => json(block(over));
    routes["GET /api/admin/settings"] = () => json({ base_url: "http://whygraph.localhost:8765", base_check: [] });
    routes["GET /api/admin/users"] = () => json([]);
    routes["GET /api/admin/orgs"] = () => json([]);
    mount("/admin");
    const card = await screen.findByTestId("admin-updates");
    await waitFor(() => expect(within(card).getByTestId("admin-updates-latest")).toBeInTheDocument());
    return card;
  }

  it("warns about manual steps and links the release notes", async () => {
    const card = await mountAdmin({ manual_steps: true });
    expect(within(card).getByTestId("admin-updates-manual")).toHaveTextContent("needs manual steps");
    expect(within(card).getByRole("link", { name: "What's new in 2.3.0" })).toHaveAttribute(
      "href",
      "https://mtrdesign.github.io/whygraph/releases/2.3.0/",
    );
  });

  it("says up to date only after a successful check", async () => {
    let card = await mountAdmin({ available: false, latest: "2.2.1" });
    expect(card).toHaveTextContent("Up to date");
    cleanup();
    card = await mountAdmin({ available: false, latest: null, enabled: false, checked_at: null });
    expect(card).toHaveTextContent("Latest version: unknown");
    expect(card).not.toHaveTextContent("Up to date");
    cleanup();
    card = await mountAdmin({ available: false, latest: null, error: "network" });
    expect(card).toHaveTextContent("Latest version: unknown");
    expect(card).toHaveTextContent("Last check failed");
  });

  it("shows the instance's update status", async () => {
    state = {
      mode: "production",
      host_kind: "base",
      base_url: "http://whygraph.localhost:8765",
      setup_complete: true,
      bootstrap_required: false,
      user: { uid: "u1", display_name: "Ada", email: "a@example.com", role: null, is_instance_admin: true },
      org: null,
    };
    routes["GET /api/admin/update"] = () => json(block());
    routes["GET /api/admin/settings"] = () => json({ base_url: "http://whygraph.localhost:8765", base_check: [] });
    routes["GET /api/admin/users"] = () => json([]);
    routes["GET /api/admin/orgs"] = () => json([]);
    mount("/admin");
    const card = await screen.findByTestId("admin-updates");
    await waitFor(() => expect(card).toHaveTextContent("Latest version: 2.3.0"));
    expect(card).toHaveTextContent("Current version: 2.2.1");
    expect(within(card).getByRole("link", { name: "How to upgrade" })).toHaveAttribute(
      "href",
      "https://mtrdesign.github.io/whygraph/deploy/production/#upgrading",
    );
  });
});
