import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Note } from "../api";
import { MessageBubble, type AssistantTurn } from "../components/chat/MessageBubble";
import { Markdown } from "../components/chat/Markdown";
import { NotesTab } from "../components/NotesTab";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { proposedNoteId } from "../lib/notes";
import { ProjectProvider } from "../lib/project";
import { parseSearch, stringifySearch, createAppRouter } from "../router";
import { useUi } from "../store";
import { ThemeProvider } from "../theme";

// ---- a fake portal with a notes store -----------------------------------------------

interface Fake {
  notes: Note[];
  /** The permissions every project grants the caller. */
  permissions: string[];
  source: string;
  /** Answer to POST /confirm, when set. */
  confirmError?: { status: number; code: string };
}

let fake: Fake;
let calls: { url: string; method: string; body: unknown }[];

const VIEWER = "u1";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function note(over: Partial<Note> & { id: number }): Note {
  return {
    kind: "decision",
    body: `Body of note ${over.id}`,
    status: "proposed",
    anchor: { kind: "symbol", qualified_name: "pkg.mod.fn", path: "src/mod.py", line_start: 3, line_end: 9 },
    anchor_state: "n/a",
    source: "human",
    origin: "ui",
    proposed_by: "u2",
    proposed_by_name: "Grace",
    proposed_at: "2026-10-01T10:00:00Z",
    refs: [],
    ...over,
  };
}

function details(slug: string) {
  return {
    slug,
    name: `Project ${slug}`,
    source: fake.source,
    root: `/repos/${slug}`,
    remote_url: null,
    initialized: true,
    initialized_at: "2026-01-01T00:00:00Z",
    last_scan_at: null,
    created_at: "2026-01-01T00:00:00Z",
    root_status: "ok",
    running_scan: null,
    restricted: false,
    my_role: "admin",
    permissions: fake.permissions,
    stale: null,
    agents: [],
    missing_key: null,
    mcp_url: `http://127.0.0.1:8765/mcp/${slug}`,
    stats: null,
  };
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  calls.push({ url: url.pathname + url.search, method, body });
  const path = url.pathname;
  if (path === "/api/portal/state") {
    return Promise.resolve(
      json({
        mode: "local",
        setup_complete: true,
        user: { uid: VIEWER, display_name: "Ada", role: "owner" },
        port: 8765,
        shared_folders: [],
        usage: null,
      }),
    );
  }
  if (path === "/api/projects") return Promise.resolve(json({ projects: [details("alpha")] }));
  const m = /^\/api\/projects\/alpha(\/.*)?$/.exec(path);
  if (!m) return Promise.resolve(json({ error: "unhandled" }, 500));
  const rest = m[1];
  if (!rest) return Promise.resolve(json(details("alpha")));
  if (rest === "/chat/sessions") return Promise.resolve(json([]));
  if (rest === "/notes/summary") {
    const proposed = fake.notes.filter((n) => n.status === "proposed");
    return Promise.resolve(
      json({
        proposed: proposed.length,
        proposed_by_agents: proposed.filter((n) => n.source !== "human").length,
        confirmed: fake.notes.filter((n) => n.status === "confirmed").length,
      }),
    );
  }
  if (rest === "/notes/for-target") return Promise.resolve(json(fake.notes));
  if (rest === "/notes" && method === "GET") {
    const status = url.searchParams.get("status");
    const attention = url.searchParams.get("attention") === "true";
    const before = Number(url.searchParams.get("before_id") ?? Infinity);
    let rows = fake.notes.filter((n) => n.id < before);
    if (attention) rows = rows.filter((n) => n.status === "confirmed" && ["changed", "missing"].includes(n.anchor_state));
    else if (status) rows = rows.filter((n) => n.status === status);
    const limit = Number(url.searchParams.get("limit") ?? 100);
    return Promise.resolve(json([...rows].sort((a, b) => b.id - a.id).slice(0, limit)));
  }
  const one = /^\/notes\/(\d+)(\/\w+)?$/.exec(rest);
  if (one) {
    const found = fake.notes.find((n) => n.id === Number(one[1]));
    if (!found) return Promise.resolve(json({ error: "gone", code: "note_not_found" }, 404));
    if (method === "GET") return Promise.resolve(json({ ...found, superseded_by_ids: [] }));
    if (one[2] === "/confirm" && fake.confirmError) {
      return Promise.resolve(json({ error: "second", code: fake.confirmError.code }, fake.confirmError.status));
    }
    if (one[2] === "/confirm") found.status = "confirmed";
    if (one[2] === "/retire") found.status = "retired";
    return Promise.resolve(json(found));
  }
  if (rest === "/notes/reject-agent-proposals") return Promise.resolve(json({ rejected: 2 }));
  if (rest === "/notes" && method === "POST") return Promise.resolve(json(note({ id: 99 }), 201));
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
  return { router, queryClient };
}

/** Renders `ui` as a project's component, inside a router for its links. */
async function renderIn(ui: React.ReactNode, path = "/p/alpha/chat/7") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
  const root = createRootRoute();
  const page = createRoute({
    getParentRoute: () => root,
    path: "/p/$slug/chat/{-$id}",
    component: () => <ProjectProvider slug="alpha">{ui}</ProjectProvider>,
  });
  const router = createRouter({
    routeTree: root.addChildren([page]),
    history: createMemoryHistory({ initialEntries: [path] }),
    parseSearch,
    stringifySearch,
  });
  render(
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
  await waitFor(() => expect(document.body.textContent).not.toBe(""));
}

beforeEach(() => {
  fake = { notes: [], permissions: PROJECT_ACTIONS, source: "local" };
  calls = [];
  window.localStorage.clear();
  useUi.setState({ paletteOpen: false, navOpen: false });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const posts = (suffix: string) => calls.filter((c) => c.method === "POST" && c.url.endsWith(suffix));

// ---- the Notes page --------------------------------------------------------------

describe("Notes page", () => {
  it("lists what waits for review, marks agent proposals and links a chat origin", async () => {
    fake.notes = [
      note({ id: 1 }),
      note({ id: 2, source: "chat", origin: "chat:12", proposed_by: VIEWER, proposed_by_name: "Ada" }),
      note({ id: 3, source: "mcp", origin: "mcp:cursor" }),
      note({ id: 4, status: "confirmed" }),
    ];
    mount("/p/alpha/notes");
    const list = await screen.findByTestId("notes-list");
    expect(within(list).getAllByRole("article")).toHaveLength(3);
    expect(screen.queryByTestId("note-4")).toBeNull();
    expect(within(screen.getByTestId("note-2")).getByText("Chat assistant")).toBeInTheDocument();
    expect(within(screen.getByTestId("note-2")).getByRole("link", { name: /From chat #12/ })).toHaveAttribute(
      "href",
      "/p/alpha/chat/12",
    );
    // Someone else's chat origin is plain text, not a link into their session.
    expect(within(screen.getByTestId("note-3")).getByText("Agent over MCP")).toBeInTheDocument();
    expect(within(screen.getByTestId("note-3")).queryByRole("link")).toBeNull();
    // No confirm-all, anywhere.
    expect(screen.queryByRole("button", { name: /confirm all/i })).toBeNull();
  });

  it("confirms one note", async () => {
    fake.notes = [note({ id: 1 })];
    mount("/p/alpha/notes");
    const card = await screen.findByTestId("note-1");
    await userEvent.click(within(card).getByRole("button", { name: "Confirm" }));
    await userEvent.click(within(card).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(posts("/notes/1/confirm")).toHaveLength(1));
    // Untouched text: the empty body, so the server records no edit.
    expect(posts("/notes/1/confirm")[0].body).toEqual({});
  });

  it("sends the edited text on confirm", async () => {
    fake.notes = [note({ id: 1 })];
    mount("/p/alpha/notes");
    const card = await screen.findByTestId("note-1");
    await userEvent.click(within(card).getByRole("button", { name: "Confirm" }));
    const text = within(card).getByLabelText("Note text");
    await userEvent.clear(text);
    await userEvent.type(text, "Better words");
    await userEvent.click(within(card).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(posts("/notes/1/confirm")).toHaveLength(1));
    expect(posts("/notes/1/confirm")[0].body).toMatchObject({ body: "Better words", kind: "decision" });
  });

  it("shows 'edited on confirm' on a confirmed note", async () => {
    fake.notes = [note({ id: 1, status: "confirmed", edited_on_confirm: true, anchor_state: "current" })];
    mount("/p/alpha/notes?filter=confirmed");
    expect(await screen.findByTestId("edited-on-confirm")).toHaveTextContent("edited on confirm");
  });

  it("rejects all from an origin after a dialog that names the count", async () => {
    fake.notes = [
      note({ id: 1, source: "mcp", origin: "mcp:cursor" }),
      note({ id: 2, source: "mcp", origin: "mcp:cursor" }),
      note({ id: 3, source: "mcp", origin: "mcp:other" }),
    ];
    mount("/p/alpha/notes");
    const card = await screen.findByTestId("note-1");
    await userEvent.click(within(card).getByRole("button", { name: "Reject all from this origin" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("2 proposals");
    await userEvent.click(within(dialog).getByRole("button", { name: "Reject 2 proposals" }));
    await waitFor(() => expect(posts("/notes/reject-agent-proposals")).toHaveLength(1));
    expect(posts("/notes/reject-agent-proposals")[0].body).toEqual({ proposed_by: "u2", origin: "mcp:cursor" });
  });

  it("asks for lines before Still true on a changed range note", async () => {
    fake.notes = [
      note({
        id: 5,
        status: "confirmed",
        anchor: { kind: "range", path: "src/a.py", line_start: 10, line_end: 20 },
        anchor_state: "changed",
      }),
    ];
    mount("/p/alpha/notes?filter=attention");
    expect(calls.some((c) => c.url.includes("attention=true"))).toBe(false);
    const card = await screen.findByTestId("note-5");
    expect(calls.some((c) => c.url.includes("attention=true"))).toBe(true);
    await userEvent.click(within(card).getByRole("button", { name: "Still true" }));
    const picker = within(card).getByTestId("line-picker");
    const [from, to] = within(picker).getAllByRole("spinbutton");
    fireEvent.change(from, { target: { value: "12" } });
    fireEvent.change(to, { target: { value: "22" } });
    await userEvent.click(within(card).getByRole("button", { name: "Still true" }));
    await waitFor(() => expect(posts("/notes/5/reconfirm")).toHaveLength(1));
    expect(posts("/notes/5/reconfirm")[0].body).toEqual({ line_start: 12, line_end: 22 });
  });

  it("revises with a new proposal that supersedes the note, and retires with a reason", async () => {
    fake.notes = [note({ id: 6, status: "confirmed", anchor_state: "current" })];
    mount("/p/alpha/notes?filter=confirmed");
    const card = await screen.findByTestId("note-6");
    await userEvent.click(within(card).getByRole("button", { name: "Revise" }));
    const text = within(card).getByLabelText("Note text");
    await userEvent.clear(text);
    await userEvent.type(text, "New wording");
    await userEvent.click(within(card).getByRole("button", { name: "Propose revision" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/notes"))).toBe(true));
    const revise = calls.find((c) => c.method === "POST" && c.url.endsWith("/notes"));
    expect(revise?.body).toMatchObject({
      body: "New wording",
      supersedes_id: 6,
      anchor: { kind: "symbol", qualified_name: "pkg.mod.fn" },
    });
    await userEvent.click(within(card).getByRole("button", { name: "Retire" }));
    await userEvent.click(within(card).getByRole("button", { name: "Retire" }));
    await waitFor(() => expect(posts("/notes/6/retire")).toHaveLength(1));
    expect(posts("/notes/6/retire")[0].body).toEqual({ reason: "outdated" });
  });

  it("replaces Confirm on a project note the viewer proposed unless they can configure", async () => {
    fake.notes = [note({ id: 7, anchor: { kind: "project" }, proposed_by: VIEWER, proposed_by_name: "Ada" })];
    fake.permissions = ["project.read", "project.chat"];
    mount("/p/alpha/notes");
    const card = await screen.findByTestId("note-7");
    expect(within(card).getByTestId("needs-second")).toHaveTextContent("Needs another member or an admin to confirm");
    expect(within(card).queryByRole("button", { name: "Confirm" })).toBeNull();
  });

  it("offers a project note's text for editing only with project.configure", async () => {
    fake.notes = [note({ id: 8, anchor: { kind: "project" } })];
    fake.permissions = ["project.read", "project.chat"];
    mount("/p/alpha/notes");
    const card = await screen.findByTestId("note-8");
    await userEvent.click(within(card).getByRole("button", { name: "Confirm" }));
    expect(within(card).queryByLabelText("Note text")).toBeNull();
  });

  it("is read-only for a viewer", async () => {
    fake.notes = [note({ id: 1 }), note({ id: 2, source: "mcp", origin: "mcp:x" })];
    fake.permissions = ["project.read"];
    mount("/p/alpha/notes");
    await screen.findByTestId("note-1");
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Reject all/ })).toBeNull();
  });

  it("opens a deep-linked note and pages with before_id", async () => {
    fake.notes = Array.from({ length: 51 }, (_, i) => note({ id: i + 1 }));
    mount("/p/alpha/notes?id=3");
    const linked = await screen.findByTestId("linked-note");
    expect(await within(linked).findByTestId("note-3")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    // The first page is ids 51..2; the next page starts before the smallest id seen.
    await waitFor(() => expect(calls.some((c) => c.url.includes("before_id=2"))).toBe(true));
  });
});

// ---- the Explorer tab ----------------------------------------------------------------

describe("Notes tab", () => {
  it("reads the notes of the symbol and lets a contributor add one", async () => {
    fake.notes = [note({ id: 1 }), note({ id: 2, status: "confirmed", anchor_state: "missing" })];
    await renderIn(<NotesTab qualifiedName="pkg.mod.fn" />);
    expect(await screen.findByTestId("note-1")).toBeInTheDocument();
    expect(within(screen.getByTestId("note-2")).getByTestId("anchor-state")).toHaveTextContent("Anchor missing");
    expect(calls.some((c) => c.url === "/api/projects/alpha/notes/for-target?qualified_name=pkg.mod.fn")).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    await userEvent.type(screen.getByLabelText("Note text"), "Never retry here");
    await userEvent.click(screen.getByRole("button", { name: "Propose note" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/notes"))).toBe(true));
    expect(calls.find((c) => c.method === "POST" && c.url.endsWith("/notes"))?.body).toMatchObject({
      body: "Never retry here",
      anchor: { kind: "symbol", qualified_name: "pkg.mod.fn" },
    });
  });

  it("is read-only for a viewer", async () => {
    fake.notes = [note({ id: 1 })];
    fake.permissions = ["project.read"];
    await renderIn(<NotesTab qualifiedName="pkg.mod.fn" />);
    await screen.findByTestId("note-1");
    expect(screen.queryByRole("button", { name: "Add note" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });
});

// ---- the Chat proposal card --------------------------------------------------------------

function proposeTurn(result: string): AssistantTurn {
  return {
    kind: "assistant",
    segments: [""],
    activityGroups: [[{ id: "t1", name: "propose_note", arguments: {}, result, running: false }]],
  };
}

describe("Chat proposal card", () => {
  it("reads the note id from the first 100 characters of the result", () => {
    expect(proposedNoteId('{"note_id": 42, "status": "proposed"}')).toBe(42);
    expect(proposedNoteId('{"status":"not_proposed","note":"budget used"}')).toBeNull();
    expect(proposedNoteId('{"error":"x"}')).toBeNull();
    expect(proposedNoteId(`${" ".repeat(100)}{"note_id": 4}`)).toBeNull();
  });

  it("is built from GET /notes/{id}, not the streamed text, and confirms with an empty body", async () => {
    const long = "x".repeat(900);
    fake.notes = [note({ id: 42, body: long, source: "chat", origin: "chat:7", proposed_by: VIEWER, proposed_by_name: "Ada" })];
    await renderIn(<MessageBubble turn={proposeTurn('{"note_id": 42, "status": "proposed", "note": {"body": "cut off')} />);
    const card = await screen.findByTestId("note-card");
    expect(within(card).getByTestId("note-card-body")).toHaveTextContent(long);
    expect(card).toHaveTextContent("The assistant wrote this. Confirm only what you said or know is true.");
    expect(card).toHaveTextContent("Everyone who can open this project can see it.");
    expect(calls.some((c) => c.url === "/api/projects/alpha/notes/42")).toBe(true);
    await userEvent.click(within(card).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(posts("/notes/42/confirm")).toHaveLength(1));
    expect(posts("/notes/42/confirm")[0].body).toEqual({});
    // The refetch shows the new status; Confirm is no longer available.
    await waitFor(() => expect(within(screen.getByTestId("note-card")).getByRole("button", { name: "Confirm" })).toBeDisabled());
    expect(within(screen.getByTestId("note-card")).getByText("Confirmed")).toBeInTheDocument();
  });

  it("shows the current status for a reloaded session", async () => {
    fake.notes = [note({ id: 42, status: "retired", source: "chat", origin: "chat:7", proposed_by: VIEWER })];
    await renderIn(<MessageBubble turn={proposeTurn('{"note_id":42,"status":"proposed"}')} />);
    const card = await screen.findByTestId("note-card");
    expect(within(card).getByText("Retired")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Confirm" })).toBeDisabled();
    expect(within(card).getByRole("button", { name: "Discard" })).toBeDisabled();
  });

  it("discards by retiring as rejected", async () => {
    fake.notes = [note({ id: 42, source: "chat", origin: "chat:7", proposed_by: VIEWER })];
    await renderIn(<MessageBubble turn={proposeTurn('{"note_id":42}')} />);
    await userEvent.click(await screen.findByRole("button", { name: "Discard" }));
    await waitFor(() => expect(posts("/notes/42/retire")).toHaveLength(1));
    expect(posts("/notes/42/retire")[0].body).toEqual({ reason: "rejected" });
  });

  it("offers Review instead of Confirm for a project-wide note", async () => {
    fake.notes = [note({ id: 42, anchor: { kind: "project" }, source: "chat", origin: "chat:7", proposed_by: VIEWER })];
    await renderIn(<MessageBubble turn={proposeTurn('{"note_id":42}')} />);
    const card = await screen.findByTestId("note-card");
    expect(within(card).queryByRole("button", { name: "Confirm" })).toBeNull();
    expect(within(card).getByTestId("note-card-scope")).toHaveTextContent("shown at the start of every agent session");
    expect(within(card).getByRole("link", { name: "Review" })).toHaveAttribute("href", "/p/alpha/notes?id=42");
  });

  it("says another member already proposed a deduped note, with no Confirm", async () => {
    fake.notes = [note({ id: 42, source: "chat", origin: "chat:3", proposed_by: "u2", proposed_by_name: "Grace" })];
    await renderIn(<MessageBubble turn={proposeTurn('{"note_id":42,"duplicate":true}')} />);
    const card = await screen.findByTestId("note-card");
    expect(within(card).getByTestId("note-card-foreign")).toHaveTextContent("Already proposed by Grace");
    expect(card).not.toHaveTextContent("The assistant wrote this");
    expect(within(card).queryByRole("button", { name: "Confirm" })).toBeNull();
    expect(within(card).getByRole("link", { name: "Review" })).toBeInTheDocument();
  });

  it("shows no card without a note id, nor any action to a viewer", async () => {
    await renderIn(<MessageBubble turn={proposeTurn('{"status":"not_proposed","note":"budget used"}')} />);
    expect(screen.queryByTestId("note-card")).toBeNull();
  });

  it("hides every action from a viewer", async () => {
    fake.notes = [note({ id: 42, source: "chat", origin: "chat:7", proposed_by: VIEWER })];
    fake.permissions = ["project.read"];
    await renderIn(<MessageBubble turn={proposeTurn('{"note_id":42}')} />);
    const card = await screen.findByTestId("note-card");
    expect(within(card).queryByRole("button")).toBeNull();
    expect(within(card).queryByRole("link", { name: "Review" })).toBeNull();
  });

  it("falls back to Review when the server says a note needs a second confirmer", async () => {
    fake.notes = [note({ id: 42, anchor: { kind: "path", path: "src" }, source: "chat", origin: "chat:7", proposed_by: VIEWER })];
    fake.confirmError = { status: 403, code: "note_needs_second_confirmer" };
    await renderIn(<MessageBubble turn={proposeTurn('{"note_id":42}')} />);
    await userEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("link", { name: "Review" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });
});

// ---- nav, Overview, Markdown ----------------------------------------------------------------

describe("project nav and Overview", () => {
  it("shows a Notes item with the waiting count", async () => {
    fake.notes = [note({ id: 1 }), note({ id: 2 }), note({ id: 3, status: "confirmed" })];
    mount("/p/alpha");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    const link = await within(nav).findByRole("link", { name: /Notes/ });
    expect(link).toHaveAttribute("href", "/p/alpha/notes");
    await waitFor(() => expect(within(link).getByTestId("nav-badge")).toHaveTextContent("2"));
  });

  it("shows no Notes item, no card and no summary call for a linked project", async () => {
    fake.source = "platform";
    mount("/p/alpha");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    await within(nav).findByRole("link", { name: "Overview" });
    expect(within(nav).queryByRole("link", { name: /Notes/ })).toBeNull();
    expect(screen.queryByTestId("notes-card")).toBeNull();
    expect(calls.some((c) => c.url.includes("/notes"))).toBe(false);
  });

  it("shows the Overview card with the counts, linking to the page", async () => {
    fake.notes = [note({ id: 1 }), note({ id: 2, source: "mcp", origin: "mcp:x" }), note({ id: 3, status: "confirmed" })];
    mount("/p/alpha");
    const card = await screen.findByTestId("notes-card");
    expect(within(card).getByTestId("notes-counts")).toHaveTextContent("2 notes waiting for review (1 from agents), 1 confirmed.");
    expect(within(card).getByRole("link")).toHaveAttribute("href", "/p/alpha/notes");
  });
});

describe("Markdown images", () => {
  it("never loads a model-supplied image; it shows the alt text and a plain link", () => {
    const { container } = render(<Markdown>{"![the diagram](https://evil.example/p.png?d=secret) and ![x](data:image/png;base64,AAAA)"}</Markdown>);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("link", { name: "the diagram" })).toHaveAttribute("href", "https://evil.example/p.png?d=secret");
    expect(container.textContent).toContain("x");
  });
});
