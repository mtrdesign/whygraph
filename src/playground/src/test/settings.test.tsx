import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { inheritedModelText, keyTestText, projectKeyStatus, tasksWithoutKey } from "../lib/settings";
import { createAppRouter } from "../router";
import { ThemeProvider } from "../theme";
import { useUi } from "../store";

// S19 - the settings pages (plan sections 4.13, 6.4): the section list and its
// deep links, per-section Save / Discard with the leave guard, key cards (status,
// last used, Test, Replace / Remove with a confirmation), inherited values, the
// read-only notices, connected portals with the revoked list, the project budget
// form and an importing project. A handler table keyed `METHOD /path` stands in
// for the backend.

type Json = Record<string, unknown>;
type Handler = (body: Json | null, url: URL) => unknown;

let handlers: Record<string, Handler>;
let log: { method: string; path: string; body: Json | null }[];

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const noKey = { set: false, hint: null };
const secrets = (llm: Json = {}, github: Json = noKey) => ({
  llm: { anthropic: noKey, openai: noKey, deepseek: noKey, openrouter: noKey, ...llm },
  github_token: github,
});
const IMPORT = { found: false, error: null, secrets_moved: [], dropped: [], custom_db_paths: [], warnings: [] };

function project(over: Json = {}): Json {
  return {
    slug: "alpha",
    name: "Alpha",
    source: "local",
    root: "/repos/alpha",
    remote_url: null,
    initialized: true,
    initialized_at: "2026-01-01T00:00:00+00:00",
    last_scan_at: "2026-01-02T00:00:00+00:00",
    created_at: "2026-01-01T00:00:00+00:00",
    root_status: "ok",
    running_scan: null,
    restricted: false,
    my_role: "admin",
    permissions: PROJECT_ACTIONS,
    last_scan_status: "ok",
    stale: null,
    agents: ["claude"],
    missing_key: null,
    mcp_url: "http://127.0.0.1:8765/mcp/alpha",
    detected: { existing_db: false, managed_hooks: [], detected_agents: [], custom_db_paths: [] },
    stats: null,
    ...over,
  };
}

const LOCAL_STATE = {
  mode: "local",
  setup_complete: true,
  user: { uid: "u1", display_name: "Ada", role: "owner" },
  port: 8765,
  shared_folders: ["/repos"],
  version: "2.0.0",
};

function prodState(role: string): Json {
  return {
    mode: "production",
    host_kind: "org",
    base_url: "http://whygraph.localhost:8765",
    setup_complete: true,
    bootstrap_required: false,
    user: { uid: "u1", display_name: "Ada", email: "ada@example.com", role: null, is_instance_admin: role === "reader" },
    org: { slug: "acme", name: "Acme", role, default_project_role: "contributor" },
  };
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const method = init?.method ?? "GET";
  const body = init?.body ? (JSON.parse(String(init.body)) as Json) : null;
  log.push({ method, path: url.pathname + url.search, body });
  const handler = handlers[`${method} ${url.pathname}`];
  if (!handler) return Promise.resolve(json({ error: `unhandled ${method} ${url.pathname}` }, 500));
  const out = handler(body, url);
  if (out instanceof Response) return Promise.resolve(out);
  return Promise.resolve(json(out));
}

const calls = (method: string, path: string) => log.filter((c) => c.method === method && c.path === path);

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

const region = (name: string) => screen.getByRole("region", { name });
const keyCard = (provider: string) => screen.getByTestId(`key-${provider}`);

// A controllable IntersectionObserver: the test says which sections are on screen.
let observers: { cb: IntersectionObserverCallback; els: Element[] }[];
class FakeObserver {
  els: Element[] = [];
  constructor(public cb: IntersectionObserverCallback) {
    observers.push(this);
  }
  observe(el: Element) {
    this.els.push(el);
  }
  disconnect() {
    this.els = [];
  }
  unobserve() {}
  takeRecords() {
    return [];
  }
}
function showOnly(id: string) {
  for (const o of observers) {
    const entries = o.els.map((el) => ({ target: el, isIntersecting: el.id === `settings-${id}` }));
    o.cb(entries as unknown as IntersectionObserverEntry[], o as unknown as IntersectionObserver);
  }
}

let scrolled: string[];

beforeEach(() => {
  log = [];
  observers = [];
  scrolled = [];
  handlers = {
    "GET /api/portal/state": () => LOCAL_STATE,
    "GET /api/projects": () => ({ projects: [project()] }),
    "GET /api/projects/alpha": () => project(),
    "GET /api/projects/alpha/config": () => ({
      config: {},
      secrets: secrets(),
      import: IMPORT,
      read_only: false,
      can_test_keys: false,
    }),
    "GET /api/portal/defaults": () => ({ config: {}, secrets: secrets(), no_provider_key: false, read_only: false }),
    "PUT /api/projects/alpha/config": (body) => ({ config: body?.config ?? {}, secrets: secrets(), import: IMPORT }),
    "PATCH /api/projects/alpha": (body) => project({ name: body?.name }),
    "POST /api/projects/alpha/init": (body) => ({
      dry_run: body?.dry_run === true,
      gitignore_added: [],
      hooks: null,
      hooks_error: null,
      agent_files: [],
      asset_files: [],
      configured_agents: (body?.agents as string[]) ?? [],
      needs_confirmation: [],
      refused: [],
      marker_written: false,
      initialized: body?.dry_run !== true,
      custom_db_paths: [],
    }),
    "GET /api/projects/alpha/usage": () => ({
      range: { from: "2026-10-01", to: "2026-11-01" },
      totals: {
        calls: 0,
        input_tokens: 0,
        output_tokens: 0,
        cache_read_tokens: null,
        cache_write_tokens: null,
        reasoning_tokens: null,
        cost_usd: 0,
        unpriced_calls: 0,
      },
      split: { interactive: { calls: 0, cost_usd: 0 }, scans: { calls: 0, cost_usd: 0 } },
      series: [],
      members: [],
    }),
  };
  window.localStorage.clear();
  window.sessionStorage.clear();
  useUi.setState({ paletteOpen: false, navOpen: false });
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })),
  );
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  vi.stubGlobal("IntersectionObserver", FakeObserver);
  Element.prototype.scrollIntoView = function (this: Element) {
    scrolled.push(this.id);
  };
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (Element.prototype as Partial<Element>).scrollIntoView;
});

// ---- the section list ---------------------------------------------------------------------------

describe("SettingsLayout", () => {
  it("lists a local project's sections under the plan's ids, in page order", async () => {
    mount("/p/alpha/settings");
    const nav = await screen.findByRole("navigation", { name: "Settings sections" });
    await screen.findByTestId("config-form");
    expect(within(nav).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "General",
      "Models and keys",
      "GitHub",
      "Git hooks",
      "Usage",
      "Agents",
      "Danger zone",
    ]);
    for (const id of ["general", "models", "github", "hooks", "budgets", "agents", "danger"]) {
      expect(document.getElementById(`settings-${id}`)).not.toBeNull();
    }
  });

  it("scrolls a ?section= deep link into view and marks it active", async () => {
    mount("/p/alpha/settings?section=danger");
    await screen.findByRole("region", { name: "Danger zone" });
    await waitFor(() => expect(scrolled).toContain("settings-danger"));
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(nav).getByRole("button", { name: "Danger zone" })).toHaveAttribute("aria-current", "true");
  });

  it("follows the scroll position, and a click scrolls to the section", async () => {
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    act(() => showOnly("github"));
    expect(within(nav).getByRole("button", { name: "GitHub" })).toHaveAttribute("aria-current", "true");
    expect(within(nav).getByRole("button", { name: "General" })).not.toHaveAttribute("aria-current");
    await user.click(within(nav).getByRole("button", { name: "Agents" }));
    expect(scrolled).toContain("settings-agents");
    expect(within(nav).getByRole("button", { name: "Agents" })).toHaveAttribute("aria-current", "true");
  });

  it("gives local global settings General, Models and keys, GitHub token and Budgets", async () => {
    mount("/settings");
    const nav = await screen.findByRole("navigation", { name: "Settings sections" });
    await screen.findByTestId("config-form");
    expect(within(nav).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "General",
      "Models and keys",
      "GitHub token",
      "Budgets",
    ]);
    expect(within(region("Budgets")).getByRole("link", { name: "Open budgets" })).toHaveAttribute(
      "href",
      "/usage?tab=budgets",
    );
    // The global GitHub token is tested per project, never here.
    expect(within(keyCard("github")).getByText(/Tested per project/)).toBeInTheDocument();
    expect(within(keyCard("github")).queryByRole("button", { name: "Test" })).toBeNull();
  });
});

// ---- section forms ------------------------------------------------------------------------------

describe("SectionForm", () => {
  it("stages an edit: Save and Discard wake up, Discard puts the value back", async () => {
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    const general = within(await screen.findByRole("region", { name: "General" }));
    const input = general.getByLabelText("Display name");
    expect(general.getByRole("button", { name: "Save" })).toBeDisabled();
    await user.type(input, " team");
    expect(general.getByRole("button", { name: "Save" })).toBeEnabled();
    await user.click(general.getByRole("button", { name: "Discard" }));
    expect(input).toHaveValue("Alpha");
    expect(general.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(calls("PATCH", "/api/projects/alpha")).toHaveLength(0);
  });

  it("saves one section only: another section's edit stays staged", async () => {
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    await user.type(screen.getByLabelText("Default model model"), "x");
    await user.click(within(region("Git hooks")).getByRole("checkbox", { name: /post-merge/ }));
    await user.click(within(region("Git hooks")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("PUT", "/api/projects/alpha/config")).toHaveLength(1));
    // Only [scan].hooks changed; the unsaved model is not in the layer.
    expect(calls("PUT", "/api/projects/alpha/config")[0].body).toEqual({
      config: { scan: { hooks: ["post-commit", "post-rewrite", "post-checkout"] } },
    });
    expect(await within(region("Git hooks")).findByText("Saved")).toBeInTheDocument();
    expect(screen.getByLabelText("Default model model")).toHaveValue("x");
    expect(within(region("Models and keys")).getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("asks before leaving with unsaved changes; Leave goes on", async () => {
    const user = userEvent.setup();
    const router = mount("/p/alpha/settings");
    const general = within(await screen.findByRole("region", { name: "General" }));
    await user.type(general.getByLabelText("Display name"), " team");
    act(() => void router.navigate({ to: "/" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Leave without saving?");
    expect(dialog).toHaveTextContent("Your changes to General are not saved.");
    await user.click(within(dialog).getByRole("button", { name: "Leave" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  it("never asks when nothing is staged, or for a ?section= change", async () => {
    const router = mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    act(() => void router.navigate({ to: "/p/$slug/settings", params: { slug: "alpha" }, search: { section: "models" } }));
    await waitFor(() => expect(router.state.location.search).toEqual({ section: "models" }));
    act(() => void router.navigate({ to: "/" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(screen.queryByText("Leave without saving?")).toBeNull();
  });
});

// ---- key cards ----------------------------------------------------------------------------------

describe("KeyCard", () => {
  it("says what is in effect per provider, with last used for configurers (BUG-11)", async () => {
    handlers["GET /api/projects/alpha/config"] = () => ({
      config: { llm: { openai: { base_url: "http://gw.internal/v1" } } },
      secrets: secrets({ openrouter: { set: true, hint: "...zz99" } }),
      import: IMPORT,
      read_only: false,
      can_test_keys: false,
      effective_keys: { anthropic: "org", openai: "none", deepseek: "none", openrouter: "project" },
      inherited: { anthropic: { set: true }, openai: { set: false }, deepseek: { set: false }, openrouter: { set: true } },
      key_last_used: { llm: { anthropic: new Date(Date.now() - 3 * 86400_000).toISOString(), openrouter: null }, github_token: null },
    });
    handlers["GET /api/portal/defaults"] = () => ({
      config: {},
      secrets: secrets({ anthropic: { set: true, hint: "...9f3c" }, openai: { set: true, hint: "...0000" } }),
      no_provider_key: false,
    });
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    const status = (p: string) => within(keyCard(p)).getByTestId("key-status");
    expect(status("anthropic")).toHaveTextContent("Using the portal default key ...9f3c");
    expect(status("anthropic")).toHaveTextContent("Last used 3 d ago");
    // The org has an OpenAI key, but this project points OpenAI elsewhere.
    expect(status("openai")).toHaveTextContent("No key: this project uses its own OpenAI endpoint");
    expect(status("deepseek")).toHaveTextContent(/^No key$/);
    expect(status("openrouter")).toHaveTextContent("Set ...zz99");
    expect(status("openrouter")).toHaveTextContent("Not used yet");
    // No Test without can_test_keys.
    expect(screen.queryByRole("button", { name: "Test" })).toBeNull();
  });

  it("Test answers in words, GitHub included (no_repo_access)", async () => {
    handlers["GET /api/projects/alpha/config"] = () => ({
      config: {},
      secrets: secrets({ anthropic: { set: true, hint: "...a1b2" }, openrouter: { set: true, hint: "...b2c3" } }, { set: true, hint: "...cret" }),
      import: IMPORT,
      read_only: false,
      can_test_keys: true,
      effective_keys: { anthropic: "project", openai: "none", deepseek: "none", openrouter: "project" },
      github: { remote: "acme/alpha", token: "project" },
    });
    handlers["POST /api/projects/alpha/keys/anthropic/test"] = () => ({ ok: true, result: "ok", scope_tested: "project", checked_at: "x" });
    handlers["POST /api/projects/alpha/keys/openrouter/test"] = () => ({ ok: false, result: "rejected", scope_tested: "project", checked_at: "x" });
    handlers["POST /api/projects/alpha/github-token/test"] = () => ({
      ok: false,
      result: "no_repo_access",
      scope_tested: "project",
      checked_at: "x",
    });
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    // No Test on a provider with no key in effect.
    expect(within(keyCard("openai")).queryByRole("button", { name: "Test" })).toBeNull();
    await user.click(within(keyCard("anthropic")).getByRole("button", { name: "Test" }));
    expect(await within(keyCard("anthropic")).findByTestId("key-test-result")).toHaveTextContent("Key works");
    await user.click(within(keyCard("openrouter")).getByRole("button", { name: "Test" }));
    expect(await within(keyCard("openrouter")).findByTestId("key-test-result")).toHaveTextContent(
      "The provider rejected this key",
    );
    await user.click(within(keyCard("github")).getByRole("button", { name: "Test" }));
    expect(await within(keyCard("github")).findByTestId("key-test-result")).toHaveTextContent(
      "This token can't read the repository",
    );
  });

  it("Replace and Revert act at once, each after a confirmation, with only that secret", async () => {
    handlers["GET /api/projects/alpha/config"] = () => ({
      config: {},
      secrets: secrets({ openai: { set: true, hint: "...a1b2" } }),
      import: IMPORT,
      read_only: false,
      effective_keys: { anthropic: "none", openai: "project", deepseek: "none", openrouter: "none" },
      inherited: { anthropic: { set: false }, openai: { set: true }, deepseek: { set: false }, openrouter: { set: false } },
    });
    handlers["GET /api/portal/defaults"] = () => ({
      config: {},
      secrets: secrets({ openai: { set: true, hint: "...9f3c" } }),
      no_provider_key: false,
    });
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    const card = within(keyCard("openai"));

    await user.click(card.getByRole("button", { name: "Replace" }));
    await user.type(card.getByLabelText("New OpenAI key"), "sk-new-1234");
    await user.click(card.getByRole("button", { name: "Save key" }));
    let dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Replace the OpenAI key?");
    expect(calls("PUT", "/api/projects/alpha/config")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Replace" }));
    await waitFor(() => expect(calls("PUT", "/api/projects/alpha/config")).toHaveLength(1));
    expect(calls("PUT", "/api/projects/alpha/config")[0].body).toEqual({ secrets: { llm: { openai: "sk-new-1234" } } });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    await user.click(card.getByRole("button", { name: "Revert to the portal default key" }));
    dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("The portal default key (...9f3c) will be used.");
    await user.click(within(dialog).getByRole("button", { name: "Revert to the portal default key" }));
    await waitFor(() => expect(calls("PUT", "/api/projects/alpha/config")).toHaveLength(2));
    expect(calls("PUT", "/api/projects/alpha/config")[1].body).toEqual({ secrets: { llm: { openai: null } } });
  });

  it("a project admin who is not an owner reverts without seeing the org key's tail", async () => {
    handlers["GET /api/portal/state"] = () => prodState("admin");
    handlers["GET /api/projects/alpha"] = () => project({ source: "github", root: null, github_full_name: "acme/alpha" });
    handlers["GET /api/projects/alpha/config"] = () => ({
      config: {},
      secrets: secrets({ openai: { set: true, hint: "...a1b2" } }),
      import: IMPORT,
      read_only: false,
      can_test_keys: true,
      effective_keys: { anthropic: "none", openai: "project", deepseek: "none", openrouter: "none" },
      inherited: { anthropic: { set: false }, openai: { set: true }, deepseek: { set: false }, openrouter: { set: false } },
    });
    // Org key tails are for org.configure holders only: the server sends hint null.
    handlers["GET /api/portal/defaults"] = () => ({
      config: {},
      secrets: secrets({ openai: { set: true, hint: null } }),
      no_provider_key: false,
      read_only: true,
    });
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    await user.click(within(keyCard("openai")).getByRole("button", { name: "Revert to the organization key" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("The organization's key will be used.");
    expect(dialog).not.toHaveTextContent("...");
  });
});

// ---- inherited values ---------------------------------------------------------------------------

describe("inherited values", () => {
  it("an empty field shows what it inherits from portal defaults; a set one can be reset", async () => {
    handlers["GET /api/portal/defaults"] = () => ({
      config: { llm: { model: "anthropic/claude-sonnet-4-5", openai: { base_url: "http://gw.internal/v1" } } },
      secrets: secrets(),
      no_provider_key: false,
    });
    handlers["GET /api/projects/alpha/config"] = () => ({
      config: { chat: { provider: "openai", model: "gpt-5" } },
      secrets: secrets(),
      import: IMPORT,
      read_only: false,
    });
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    expect(screen.getByLabelText("Default model model")).toHaveAttribute(
      "placeholder",
      "Inherited: claude-sonnet-4-5 (Anthropic) from portal defaults",
    );
    expect(screen.getByLabelText("OpenAI-compatible base URL")).toHaveAttribute(
      "placeholder",
      "Inherited: http://gw.internal/v1 from portal defaults",
    );
    // Provider names, not ids.
    expect(within(screen.getByLabelText("Chat provider")).getByRole("option", { name: "OpenAI" })).toBeInTheDocument();
    const models = within(region("Models and keys"));
    expect(models.getAllByTestId("overridden-here")).toHaveLength(1);
    await user.click(models.getByRole("button", { name: "Reset Chat" }));
    expect(screen.getByLabelText("Chat model")).toHaveValue("");
    expect(models.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("names the organization in production, and the default model's provider otherwise", () => {
    const org = { llm: { model: "openai/gpt-5" } };
    expect(inheritedModelText({ task: null, projectLayer: {}, orgLayer: org, mode: "production" })).toBe(
      "Inherited: gpt-5 (OpenAI) from the organization",
    );
    expect(inheritedModelText({ task: "chat", projectLayer: { llm: { model: "anthropic/x" } }, orgLayer: {}, mode: "local" })).toBe(
      "Uses this project's default model",
    );
    expect(inheritedModelText({ task: null, projectLayer: null, orgLayer: null, mode: "local" })).toBe("Provider default");
  });

  it("helper wording: key status, test results, tasks without a key", () => {
    expect(
      projectKeyStatus({ provider: "openai", own: noKey, scope: "none", endpoint: true, mode: "production" }),
    ).toBe("No key: this project uses its own OpenAI endpoint");
    expect(projectKeyStatus({ provider: "openai", own: noKey, scope: "org", endpoint: false, mode: "production" })).toBe(
      "Using the organization key",
    );
    expect(keyTestText("unreachable")).toBe("Couldn't reach the provider");
    expect(keyTestText("rate_limited")).toBe("Rate limited - try again later");
    expect(tasksWithoutKey({ chat: { provider: "openai" } }, { anthropic: { set: true, hint: null } })).toEqual([
      "Chat uses OpenAI, which has no key",
    ]);
  });
});

// ---- org / global page warnings -----------------------------------------------------------------

describe("org and global settings", () => {
  it("warns about a task whose provider has no key, in a tinted alert", async () => {
    handlers["GET /api/portal/defaults"] = () => ({
      config: { chat: { provider: "openai" } },
      secrets: secrets({ anthropic: { set: true, hint: "...a1b2" } }),
      no_provider_key: false,
    });
    mount("/settings");
    const warning = await screen.findByTestId("task-key-warnings");
    expect(warning).toHaveTextContent("Chat uses OpenAI, which has no key.");
    expect(warning.className).toContain("bg-warning-soft");
  });

  it("the no-provider-key notice is a warning alert", async () => {
    handlers["GET /api/portal/defaults"] = () => ({ config: {}, secrets: secrets(), no_provider_key: true });
    mount("/settings");
    expect((await screen.findByTestId("no-provider-key")).className).toContain("bg-warning-soft");
  });

  it("a non-owner reads the org settings in a disabled form, with no key actions", async () => {
    handlers["GET /api/portal/state"] = () => prodState("admin");
    handlers["GET /api/portal/defaults"] = () => ({
      config: {},
      secrets: secrets({ anthropic: { set: true, hint: null } }),
      no_provider_key: false,
      read_only: true,
    });
    mount("/settings");
    expect(await screen.findByTestId("settings-owner-only")).toHaveTextContent(
      "Only owners can change organization settings.",
    );
    await screen.findByTestId("config-form");
    expect(screen.getByLabelText("Default model model")).toBeDisabled();
    expect(within(keyCard("anthropic")).getByTestId("key-status")).toHaveTextContent(/^Set$/);
    expect(within(keyCard("anthropic")).queryByRole("button")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("the instance-admin reader gets its own wording", async () => {
    handlers["GET /api/portal/state"] = () => prodState("reader");
    handlers["GET /api/portal/defaults"] = () => ({ config: {}, secrets: secrets(), no_provider_key: false, read_only: true });
    mount("/settings");
    expect(await screen.findByTestId("settings-reader")).toHaveTextContent(
      "You're viewing this organization as an instance administrator. Everything here is read-only.",
    );
    expect(screen.queryByTestId("settings-owner-only")).toBeNull();
  });
});

// ---- read-only project settings, production copy, importing -------------------------------------

describe("project settings by audience and mode", () => {
  it("a viewer sees every field disabled, the notice, and no key action", async () => {
    handlers["GET /api/projects/alpha"] = () =>
      project({ my_role: "viewer", permissions: ["project.read"] });
    handlers["GET /api/projects/alpha/config"] = () => ({
      config: {},
      secrets: secrets({ anthropic: { set: true, hint: null } }),
      import: IMPORT,
      read_only: true,
      can_test_keys: false,
      effective_keys: { anthropic: "project", openai: "none", deepseek: "none", openrouter: "none" },
    });
    mount("/p/alpha/settings");
    expect(await screen.findByTestId("settings-read-only")).toHaveTextContent(
      "You can view these settings but not change them. Project admins can.",
    );
    await screen.findByTestId("config-form");
    expect(screen.getByLabelText("Display name")).toBeDisabled();
    expect(screen.getByLabelText("Default model model")).toBeDisabled();
    // SET-4: every control, including the ones `<fieldset disabled>` does not
    // reach (Base UI's Switch and Checkbox are spans).
    for (const select of Array.from(document.querySelectorAll("select"))) {
      expect(select).toBeDisabled();
    }
    const forge = screen.getByRole("switch", { name: "Fetch pull requests and issues from GitHub" });
    expect(forge).toHaveAttribute("data-disabled");
    for (const box of screen.getAllByRole("checkbox")) expect(box).toHaveAttribute("data-disabled");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(within(keyCard("anthropic")).queryByRole("button")).toBeNull();
    expect(within(keyCard("anthropic")).getByTestId("key-status")).toHaveTextContent(/^Set$/);
  });

  it("production: endpoint help without host.docker.internal, no hooks, no token card", async () => {
    handlers["GET /api/portal/state"] = () => prodState("owner");
    handlers["GET /api/projects/alpha"] = () => project({ source: "github", root: null, github_full_name: "acme/alpha" });
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    expect(document.body).not.toHaveTextContent("host.docker.internal");
    expect(screen.queryByRole("region", { name: "Git hooks" })).toBeNull();
    expect(screen.queryByTestId("key-github")).toBeNull();
  });

  it("local: the endpoint help names host.docker.internal", async () => {
    mount("/p/alpha/settings");
    await screen.findByTestId("config-form");
    expect(region("Models and keys")).toHaveTextContent("host.docker.internal");
  });

  it("an importing project says so, never folder missing or not set up", async () => {
    handlers["GET /api/portal/state"] = () => prodState("owner");
    handlers["GET /api/projects/alpha"] = () =>
      project({
        source: "github",
        root: null,
        importing: true,
        initialized: false,
        root_status: "missing",
        github_full_name: "acme/alpha",
        running_scan: { id: 3, status: "running", trigger: "initial" },
      });
    mount("/p/alpha/settings");
    expect(await screen.findByTestId("importing-notice")).toHaveTextContent("Importing acme/alpha");
    expect(screen.queryByTestId("project-unavailable")).toBeNull();
    expect(screen.queryByTestId("not-initialized")).toBeNull();
  });
});

// ---- connected portals and the budget form ------------------------------------------------------

describe("connected portals and the project budget", () => {
  it("lists revoked connections with the reason, and Revoke asks first (SET-10)", async () => {
    handlers["GET /api/portal/state"] = () => prodState("owner");
    handlers["GET /api/projects/alpha"] = () => project({ source: "github", root: null });
    handlers["GET /api/projects/alpha/access"] = () => ({ restricted: false, org_default: "viewer", people: [], invitations: [] });
    handlers["GET /api/projects/alpha/connections"] = () => [
      { uid: "c1", user_login: "grace", user_name: "Grace", client_name: "grace-mbp", created_at: "2026-10-01T00:00:00Z", last_used_at: null, revoked_at: null, revoked_reason: null },
      { uid: "c2", user_login: "sam", user_name: "Sam", client_name: "sam-air", created_at: "2026-09-01T00:00:00Z", last_used_at: null, revoked_at: "2026-10-05T00:00:00Z", revoked_reason: "admin_revoked" },
    ];
    handlers["DELETE /api/projects/alpha/connections/c1"] = () => new Response(null, { status: 204 });
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    const section = await screen.findByTestId("project-connections");
    await waitFor(() => expect(section).toHaveTextContent("grace-mbp"));
    expect(calls("GET", "/api/projects/alpha/connections?include=revoked")).not.toHaveLength(0);
    const revoked = within(section).getByTestId("revoked-connections");
    expect(revoked).toHaveTextContent("Revoked (last 30 days)");
    expect(revoked).toHaveTextContent("sam-air");
    expect(revoked).toHaveTextContent("Revoked by an admin");
    await user.click(within(section).getByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog");
    expect(calls("DELETE", "/api/projects/alpha/connections/c1")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(calls("DELETE", "/api/projects/alpha/connections/c1")).toHaveLength(1));
  });

  it("the project budget is a section form: Save sends it, Saved says so", async () => {
    handlers["PUT /api/projects/alpha/budget"] = (body) => ({ ...body, spent_usd: 0, pct: 0 });
    const user = userEvent.setup();
    mount("/p/alpha/settings");
    const budget = within(await screen.findByTestId("project-budget"));
    expect(budget.getByRole("button", { name: "Save" })).toBeDisabled();
    await user.type(budget.getByLabelText("Monthly budget (USD)"), "25");
    await user.click(budget.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("PUT", "/api/projects/alpha/budget")).toHaveLength(1));
    expect(calls("PUT", "/api/projects/alpha/budget")[0].body).toEqual({ monthly_usd: 25, hard_stop: false });
  });

  it("access rows wrap instead of squeezing the name (PH-8)", async () => {
    handlers["GET /api/portal/state"] = () => prodState("owner");
    handlers["GET /api/projects/alpha"] = () => project({ source: "github", root: null });
    handlers["GET /api/projects/alpha/access"] = () => ({
      restricted: false,
      org_default: "viewer",
      people: [{ uid: "u2", login: "meg", name: "Meg", avatar: null, org_role: "member", project_role: "contributor", source: "grant" }],
      invitations: [],
    });
    mount("/p/alpha/settings");
    const row = await screen.findByTestId("access-u2");
    expect(row.className).toContain("row-wrap");
    expect(within(row).getByRole("option", { name: "Contributor" })).toBeInTheDocument();
  });
});
