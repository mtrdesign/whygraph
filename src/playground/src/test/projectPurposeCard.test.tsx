import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRouter } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Note, PurposeState } from "../api";
import { ProjectPurposeCard } from "../components/portal/ProjectPurposeCard";
import { NOTE_KINDS, KIND_LABEL, SOURCE_LABEL } from "../lib/notes";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function note(over: Partial<Note> & { id: number }): Note {
  return {
    kind: "purpose",
    body: `Purpose ${over.id}`,
    status: "proposed",
    anchor: { kind: "project" },
    anchor_state: "n/a",
    source: "analysis",
    origin: "ui",
    proposed_by: "u1",
    proposed_at: "2026-10-31T10:00:00Z",
    ...over,
  };
}

let state: PurposeState;
let permissions: string[];
let calls: { path: string; method: string; body: unknown }[];

const base = (): PurposeState => ({
  confirmed: null,
  proposed: null,
  running: false,
  last_run: null,
  stale: null,
  can_generate: true,
  estimate: {
    model: { provider: "anthropic", model: "claude-haiku-4-5" },
    tokens: null,
    cost: { usd: 0.01, low: 0.005, high: 0.02, currency: "USD", prices_as_of: "2026-10-01" },
    missing_key: null,
  },
});

const run = (over: Partial<NonNullable<PurposeState["last_run"]>> = {}) => ({
  status: "ok",
  model: "claude-haiku-4-5",
  provider: "anthropic",
  tokens_in: 100,
  tokens_out: 20,
  cost_usd: 0.0074,
  finished_at: "2026-10-31T10:12:03Z",
  sources: ["README.md", "pyproject.toml"],
  error_code: null,
  ...over,
});

beforeEach(() => {
  state = base();
  permissions = ["project.read", "project.chat", "project.scan_full", "project.configure"];
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1:8765");
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ path: url.pathname, method, body });
      if (url.pathname === "/api/portal/state")
        return Promise.resolve(json({ mode: "local", setup_complete: true, user: { uid: "u1", role: "owner" } }));
      if (url.pathname === "/api/projects/alpha/purpose") return Promise.resolve(json(state));
      if (url.pathname === "/api/projects/alpha")
        return Promise.resolve(json({ slug: "alpha", source: "local", initialized: true, permissions }));
      if (/\/notes\/\d+\/confirm$/.test(url.pathname)) return Promise.resolve(json(note({ id: 1, status: "confirmed" })));
      return Promise.resolve(json({ error: "unhandled" }, 404));
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const root = createRootRoute({
    component: () => (
      <QueryClientProvider client={client}>
        <ProjectPurposeCard slug="alpha" />
      </QueryClientProvider>
    ),
  });
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ["/"] }) });
  await act(async () => {
    render(<RouterProvider router={router} />);
  });
  return screen.findByTestId("purpose-card");
}

describe("ProjectPurposeCard", () => {
  it("none: offers to describe the purpose, with the cost", async () => {
    const card = await mount();
    expect(card).toHaveAttribute("data-state", "none");
    expect(screen.getByRole("button", { name: /Describe the project's purpose/ })).toBeEnabled();
    expect(screen.getByTestId("purpose-cost")).toHaveTextContent("about $0.01 on Anthropic, claude-haiku-4-5");
  });

  it("hides the cost when cost_hidden", async () => {
    state.estimate = { ...state.estimate, cost: null, cost_hidden: true };
    await mount();
    expect(screen.queryByTestId("purpose-cost")).toBeNull();
  });

  it("running: no buttons, from the server's flag", async () => {
    state.running = true;
    const card = await mount();
    expect(card).toHaveAttribute("data-state", "running");
    expect(screen.getByTestId("purpose-running")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("proposed: body, sources, model, cost, Confirm and Edit (admin)", async () => {
    state.proposed = note({ id: 1, body: "Four labelled lines" });
    state.last_run = run();
    state.stale = false;
    const card = await mount();
    expect(card).toHaveAttribute("data-state", "proposed");
    expect(screen.getByText("Four labelled lines")).toBeInTheDocument();
    expect(screen.getByTestId("purpose-sources")).toHaveTextContent("README.md, pyproject.toml");
    expect(screen.getByTestId("purpose-run")).toHaveTextContent("claude-haiku-4-5");
    expect(screen.getByTestId("purpose-run")).toHaveTextContent("$0.01");
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/projects/alpha/notes/1/confirm")).toBe(true));
    expect(calls.find((c) => c.path.endsWith("/confirm"))?.body).toEqual({});
  });

  it("proposed: Edit sends the edited body through confirm; non-admins get no Edit", async () => {
    state.proposed = note({ id: 1, body: "Draft" });
    await mount();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByRole("textbox", { name: "Project purpose" });
    await user.clear(box);
    await user.type(box, "Mine");
    await user.click(screen.getByRole("button", { name: "Save and confirm" }));
    await waitFor(() => expect(calls.find((c) => c.path.endsWith("/confirm"))?.body).toEqual({ body: "Mine" }));
  });

  it("proposed: a caller without project.configure gets no Edit", async () => {
    permissions = ["project.read", "project.chat"];
    state.proposed = note({ id: 1 });
    await mount();
    await screen.findByRole("button", { name: "Confirm" });
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
  });

  it("confirmed: shows the body; Regenerate is disabled while inputs are unchanged", async () => {
    state.confirmed = note({ id: 1, body: "Settled", status: "confirmed" });
    state.last_run = run();
    state.stale = false;
    const card = await mount();
    expect(card).toHaveAttribute("data-state", "confirmed");
    expect(screen.getByTestId("purpose-confirmed")).toHaveTextContent("Settled");
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeDisabled();
  });

  it("confirmed and stale: Regenerate is enabled and posts", async () => {
    state.confirmed = note({ id: 1, status: "confirmed" });
    state.stale = true;
    await mount();
    const button = screen.getByRole("button", { name: "Regenerate" });
    expect(button).toBeEnabled();
    await userEvent.setup().click(button);
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/purpose"))).toBe(true));
  });

  it("confirmed with a pending revision shows both", async () => {
    state.confirmed = note({ id: 1, body: "Old text", status: "confirmed" });
    state.proposed = note({ id: 2, body: "New text", supersedes_id: 1 });
    const card = await mount();
    expect(card).toHaveAttribute("data-state", "revision");
    expect(screen.getByTestId("purpose-confirmed")).toHaveTextContent("Old text");
    expect(screen.getByTestId("purpose-proposed")).toHaveTextContent("New text");
    expect(screen.getByText("Pending revision")).toBeInTheDocument();
  });

  it("failed: shows the registry message for the code, never raw server text", async () => {
    state.last_run = run({ status: "failed", error_code: "purpose_no_input", sources: [] });
    const card = await mount();
    expect(card).toHaveAttribute("data-state", "failed");
    const text = screen.getByTestId("purpose-failed").textContent ?? "";
    expect(text.length).toBeGreaterThan(0);
    expect(text).not.toContain("purpose_no_input");
  });

  it("no key: explains and disables the button", async () => {
    state.estimate = { ...state.estimate, missing_key: "anthropic" };
    await mount();
    expect(screen.getByTestId("purpose-no-key")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Describe the project's purpose/ })).toBeDisabled();
  });

  it("viewer: read-only, no buttons", async () => {
    permissions = ["project.read"];
    state.can_generate = false;
    state.proposed = note({ id: 1 });
    await mount();
    await screen.findByTestId("purpose-proposed");
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("notes lib with the purpose kind", () => {
  it("labels purpose and analysis, but never offers purpose as a selectable kind", () => {
    expect(KIND_LABEL.purpose).toBe("Project purpose");
    expect(SOURCE_LABEL.analysis).toBe("Drafted by analysis");
    expect(NOTE_KINDS).toEqual(["decision", "constraint", "gotcha", "convention"]);
    expect(NOTE_KINDS).not.toContain("purpose");
  });
});
