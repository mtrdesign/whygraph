import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PROJECT_ACTIONS } from "../lib/permissions";
import { STARTER_PROMPTS } from "../components/chat/MessageThread";
import { createAppRouter } from "../router";
import { useUi } from "../store";
import { ThemeProvider } from "../theme";

// The Chats section in the sidebar, the one-column chat view and its draft session
// (M2f-3 §4.6, §6.4 "Shell" chat parts), through the real router against a fake portal.

interface Session {
  id: number;
  title: string;
  provider: string;
  model: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

interface Fake {
  projects: Record<string, Record<string, unknown>>;
  sessions: Session[];
  calls: { path: string; method: string; body: unknown; signal?: AbortSignal | null }[];
  /** The open SSE stream of the last message POST, fed by the test. */
  stream: ReadableStreamDefaultController<Uint8Array> | null;
  /** Messages the transcript returns, per session. */
  transcripts: Record<number, unknown[]>;
}
let fake: Fake;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function project(slug: string, over: Record<string, unknown> = {}) {
  return {
    slug,
    name: `Project ${slug}`,
    source: "local",
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
    permissions: PROJECT_ACTIONS,
    stale: null,
    agents: [],
    missing_key: null,
    mcp_url: null,
    // Not null: a null `stats` sends the data pages through the unsafe-path probe first.
    stats: { commits: 0 },
    ...over,
  };
}

function session(id: number, title: string, minutesAgo = id): Session {
  const at = new Date(Date.now() - minutesAgo * 60_000).toISOString();
  return { id, title, provider: "openai", model: "gpt-x", created_at: at, updated_at: at, message_count: 2 };
}

const enc = new TextEncoder();
const frame = (f: unknown) => enc.encode(`data: ${JSON.stringify(f)}\n\n`);

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://127.0.0.1:8765");
  const path = url.pathname;
  const method = init?.method ?? "GET";
  let body: unknown;
  try {
    body = init?.body ? JSON.parse(String(init.body)) : undefined;
  } catch {
    body = init?.body;
  }
  fake.calls.push({ path, method, body, signal: init?.signal });

  if (path === "/api/portal/state") {
    return Promise.resolve(
      json({
        mode: "local",
        setup_complete: true,
        user: { uid: "u1", display_name: "T", role: "owner" },
        port: 8765,
        shared_folders: [],
      }),
    );
  }
  if (path === "/api/projects") return Promise.resolve(json({ projects: Object.values(fake.projects) }));
  const m = /^\/api\/projects\/([^/]+)(\/.*)?$/.exec(path);
  if (m) {
    const [, slug, rest] = m;
    const p = fake.projects[slug];
    if (!p) return Promise.resolve(json({ error: "project not found", code: "not_found" }, 404));
    if (!rest) return Promise.resolve(json(p));
    if (rest === "/chat/providers") {
      return Promise.resolve(json([{ provider: "openai", configured: true, default_model: "gpt-x", env_var: null }]));
    }
    if (rest === "/chat/models") {
      return Promise.resolve(
        json({ provider: "openai", source: "live", default_model: "gpt-x", models: [{ id: "gpt-x", display_name: "GPT X" }] }),
      );
    }
    if (rest === "/chat/sessions" && method === "GET") return Promise.resolve(json(fake.sessions));
    if (rest === "/chat/sessions" && method === "POST") {
      const b = (body ?? {}) as { provider?: string; model?: string };
      const row = { ...session(7, "New chat", 0), provider: b.provider ?? "openai", model: b.model ?? "gpt-x", message_count: 0 };
      fake.sessions = [row, ...fake.sessions];
      return Promise.resolve(json(row, 201));
    }
    const s = /^\/chat\/sessions\/(\d+)(\/messages)?$/.exec(rest ?? "");
    if (s) {
      const id = Number(s[1]);
      if (s[2] && method === "POST") {
        // The server titles a "New chat" from its first message before the first frame.
        const content = (body as { content: string }).content;
        fake.sessions = fake.sessions.map((x) => (x.id === id && x.title === "New chat" ? { ...x, title: content } : x));
        return Promise.resolve(
          new Response(
            new ReadableStream<Uint8Array>({
              start(c) {
                fake.stream = c;
              },
            }),
            { status: 200, headers: { "content-type": "text/event-stream" } },
          ),
        );
      }
      if (method === "PATCH") {
        const title = (body as { title?: string }).title;
        fake.sessions = fake.sessions.map((x) => (x.id === id && title ? { ...x, title } : x));
        return Promise.resolve(json(fake.sessions.find((x) => x.id === id)));
      }
      if (method === "DELETE") {
        fake.sessions = fake.sessions.filter((x) => x.id !== id);
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      const row = fake.sessions.find((x) => x.id === id) ?? session(id, "t");
      return Promise.resolve(json({ ...row, messages: fake.transcripts[id] ?? [] }));
    }
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

const chats = () => screen.findByTestId("chats-section");
const sessionCalls = () => fake.calls.filter((c) => c.path.includes("/chat/sessions"));

beforeEach(() => {
  fake = {
    projects: {
      alpha: project("alpha"),
      linked: project("linked", { source: "platform", link: { status: "ok" } }),
      fresh: project("fresh", { initialized: false, initialized_at: null }),
    },
    sessions: [],
    calls: [],
    stream: null,
    transcripts: {},
  };
  window.localStorage.clear();
  useUi.setState({ paletteOpen: false, navOpen: false, streamingSessionId: null });
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })),
  );
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the Chats section", () => {
  it("replaces the Chat nav item, and says when there are no chats yet", async () => {
    mount("/p/alpha/scans");
    const section = await chats();
    await within(section).findByText("No chats yet");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).queryByRole("link", { name: "Chat" })).toBeNull();
    expect(within(section).getByRole("button", { name: "New chat" })).toBeEnabled();
  });

  it("is absent, with no sessions request, on a linked project and one not set up", async () => {
    mount("/p/linked");
    await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() => expect(fake.calls.some((c) => c.path === "/api/projects/linked")).toBe(true));
    expect(screen.queryByTestId("chats-section")).toBeNull();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).queryByRole("link", { name: "Explorer" })).toBeNull();
    document.body.innerHTML = "";

    mount("/p/fresh/settings");
    const freshNav = await screen.findByRole("navigation", { name: "Main" });
    await within(freshNav).findByRole("link", { name: "Continue setup" });
    expect(within(freshNav).getByRole("link", { name: "Continue setup" })).toHaveAttribute("href", "/p/fresh/init");
    expect(within(freshNav).getByRole("link", { name: "Settings" })).toBeInTheDocument();
    expect(within(freshNav).queryByRole("link", { name: "Explorer" })).toBeNull();
    expect(within(freshNav).queryByRole("link", { name: "Scans" })).toBeNull();
    expect(screen.queryByTestId("chats-section")).toBeNull();
    expect(sessionCalls()).toHaveLength(0);
  });

  it("lists 20 sessions with a relative date, then 20 more on Show more", async () => {
    fake.sessions = Array.from({ length: 45 }, (_, i) => session(i + 1, `Chat ${i + 1}`, i + 1));
    mount("/p/alpha/chat/3");
    const section = await chats();
    await within(section).findByText("Chat 1");
    expect(within(section).getAllByTestId("chat-row")).toHaveLength(20);
    expect(within(section).getAllByText("1 min ago").length).toBeGreaterThan(0);
    // The open session has the selected treatment and is the current page.
    const open = within(section).getByRole("link", { name: /Chat 3/ });
    expect(open).toHaveAttribute("aria-current", "page");
    expect(open.closest("li")).toHaveClass("bg-primary-soft");
    expect(open).toHaveAttribute("title", "Chat 3 (2 messages)");

    await userEvent.setup().click(within(section).getByRole("button", { name: "Show more" }));
    expect(within(section).getAllByTestId("chat-row")).toHaveLength(40);
    await userEvent.setup().click(within(section).getByRole("button", { name: "Show more" }));
    expect(within(section).getAllByTestId("chat-row")).toHaveLength(45);
    expect(within(section).queryByRole("button", { name: "Show more" })).toBeNull();
  });

  it("renames inline: Enter saves, an empty title is refused, Escape cancels", async () => {
    fake.sessions = [session(1, "Old title")];
    mount("/p/alpha/scans");
    const section = await chats();
    const user = userEvent.setup();
    await user.click(await within(section).findByRole("button", { name: "Actions for Old title" }));
    await user.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const field = await within(section).findByRole("textbox", { name: "Chat title" });
    await waitFor(() => expect(field).toHaveFocus());

    await user.clear(field);
    await user.keyboard("{Enter}");
    expect(within(section).getByRole("alert")).toHaveTextContent("Enter a title.");
    expect(fake.calls.some((c) => c.method === "PATCH")).toBe(false);

    await user.type(field, "New title{Enter}");
    await within(section).findByRole("link", { name: /New title/ });
    expect(fake.calls.find((c) => c.method === "PATCH")).toMatchObject({
      path: "/api/projects/alpha/chat/sessions/1",
      body: { title: "New title" },
    });

    await user.click(within(section).getByRole("button", { name: "Actions for New title" }));
    await user.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const again = await within(section).findByRole("textbox", { name: "Chat title" });
    await user.type(again, " changed");
    fireEvent.keyDown(again, { key: "Escape" });
    await within(section).findByRole("link", { name: /New title/ });
    expect(fake.calls.filter((c) => c.method === "PATCH")).toHaveLength(1);
  });

  it("deletes through the confirm dialog, and leaves the open session for a new chat", async () => {
    fake.sessions = [session(1, "Keep me"), session(2, "Drop me")];
    const router = mount("/p/alpha/chat/2");
    const section = await chats();
    const user = userEvent.setup();
    await user.click(await within(section).findByRole("button", { name: "Actions for Drop me" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent('Delete "Drop me"? This removes its messages for good.');
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/p/alpha/chat"));
    expect(fake.calls.some((c) => c.method === "DELETE" && c.path === "/api/projects/alpha/chat/sessions/2")).toBe(true);
    await waitFor(() => expect(within(section).queryByText("Drop me")).toBeNull());
    expect(within(section).getByText("Keep me")).toBeInTheDocument();
  });

  it("disables + under a budget stop, with the reason", async () => {
    fake.projects.alpha = project("alpha", { llm_block: "budget_exceeded", llm_block_scope: "project" });
    mount("/p/alpha/scans");
    const section = await chats();
    const plus = within(section).getByRole("button", { name: "New chat" });
    await waitFor(() => expect(plus).toBeDisabled());
    const reason = document.getElementById(plus.getAttribute("aria-describedby") ?? "");
    expect(reason).toHaveTextContent("Monthly budget reached");
  });

  it("shows a spinner on the session that is streaming", async () => {
    fake.sessions = [session(1, "Quiet"), session(2, "Busy")];
    mount("/p/alpha/scans");
    const section = await chats();
    await within(section).findByText("Busy");
    expect(within(section).queryByTestId("chat-row-streaming")).toBeNull();
    act(() => useUi.getState().setStreamingSessionId(2));
    const spinner = within(section).getByTestId("chat-row-streaming");
    expect(spinner.closest("li")).toHaveTextContent("Busy");
    act(() => useUi.getState().setStreamingSessionId(null));
    expect(within(section).queryByTestId("chat-row-streaming")).toBeNull();
  });

  it("shows an error with Retry when the list fails", async () => {
    let fail = true;
    const base = fakeFetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "http://127.0.0.1:8765").pathname;
        if (fail && path === "/api/projects/alpha/chat/sessions") return Promise.resolve(json({ error: "boom" }, 500));
        return base(input, init);
      }),
    );
    fake.sessions = [session(1, "Back again")];
    mount("/p/alpha/scans");
    const section = await chats();
    const retry = await within(section).findByRole("button", { name: "Retry" });
    fail = false;
    await userEvent.setup().click(retry);
    await within(section).findByText("Back again");
  });
});

describe("the chat view", () => {
  it("has one column with a title, an sr-only h1 and starter prompts that fill the composer", async () => {
    mount("/p/alpha/chat");
    expect(await screen.findByRole("heading", { level: 1, name: "New chat" })).toHaveClass("sr-only");
    expect(screen.getByTestId("chat-title")).toHaveTextContent("New chat");
    const empty = await screen.findByTestId("chat-empty");
    expect(empty).toHaveTextContent("Ask about this codebase");
    const user = userEvent.setup();
    await user.click(within(empty).getByRole("button", { name: STARTER_PROMPTS[1] }));
    expect(screen.getByRole("textbox")).toHaveValue(STARTER_PROMPTS[1]);
    // Opening a new chat persists nothing.
    expect(fake.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("creates the session on the first send and keeps streaming across the move to /chat/<id>", async () => {
    const router = mount("/p/alpha/chat");
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox");
    // The draft's pickers have settled on the first configured provider.
    await screen.findByRole("combobox", { name: "Provider" });
    await user.type(box, "why?");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(fake.stream).not.toBeNull());
    const creates = fake.calls.filter((c) => c.method === "POST" && c.path === "/api/projects/alpha/chat/sessions");
    expect(creates).toHaveLength(1);
    expect(creates[0].body).toEqual({ provider: "openai", model: "gpt-x" });
    const post = fake.calls.find((c) => c.path === "/api/projects/alpha/chat/sessions/7/messages");
    expect(post?.body).toEqual({ content: "why?" });
    await waitFor(() => expect(router.state.location.pathname).toBe("/p/alpha/chat/7"));
    // replace: the draft is not a history entry of its own.
    expect(router.history.length).toBe(1);

    // The thread "is thinking" until the first token; the row spins.
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();
    expect(useUi.getState().streamingSessionId).toBe(7);

    act(() => fake.stream!.enqueue(frame({ type: "text_delta", text: "Because " })));
    await screen.findByText(/Because/);
    // The first frame refreshed the list: the server's first-message title shows mid-turn.
    const section = await chats();
    await within(section).findByText("why?");
    expect(within(section).getByTestId("chat-row-streaming")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("chat-title")).toHaveTextContent("why?"));

    // The navigation did not abort the stream: later frames still land.
    expect(post?.signal?.aborted).toBe(false);
    fake.transcripts[7] = [
      { id: 1, role: "user", content: "why?", tool_calls: [], tool_call_id: null, input_tokens: null, output_tokens: null, provider: null, model: null, error: null, created_at: "" },
      { id: 2, role: "assistant", content: "Because history.", tool_calls: [], tool_call_id: null, input_tokens: 3, output_tokens: 4, provider: "openai", model: "gpt-x", error: null, created_at: "" },
    ];
    act(() => {
      fake.stream!.enqueue(frame({ type: "text_delta", text: "history." }));
      fake.stream!.enqueue(frame({ type: "done", message_id: 2, input_tokens: 3, output_tokens: 4, finish_reason: "stop" }));
      fake.stream!.close();
    });
    await screen.findByText("Because history.");
    expect(post?.signal?.aborted).toBe(false);
    await waitFor(() => expect(useUi.getState().streamingSessionId).toBeNull());
    expect(fake.calls.filter((c) => c.method === "POST" && c.path.endsWith("/chat/sessions"))).toHaveLength(1);
  });

  it("does abort when the person opens another session mid-stream", async () => {
    fake.sessions = [session(3, "Other")];
    const router = mount("/p/alpha/chat");
    const user = userEvent.setup();
    await screen.findByRole("combobox", { name: "Provider" });
    await user.type(await screen.findByRole("textbox"), "first");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/p/alpha/chat/7"));
    const post = fake.calls.find((c) => c.path === "/api/projects/alpha/chat/sessions/7/messages");
    await act(() => router.navigate({ to: "/p/$slug/chat/{-$id}", params: { slug: "alpha", id: "3" } }));
    await waitFor(() => expect(post?.signal?.aborted).toBe(true));
  });
});

describe("palette and shortcuts", () => {
  it("offer New chat and Explorer on a normal project", async () => {
    useUi.setState({ paletteOpen: true });
    mount("/p/alpha/scans");
    await screen.findByRole("option", { name: "New chat" });
    expect(screen.getByRole("option", { name: "Explorer" })).toBeInTheDocument();
  });

  it("offer neither on a linked project (BUG-18)", async () => {
    useUi.setState({ paletteOpen: true });
    mount("/p/linked");
    await screen.findByRole("option", { name: "Scans" });
    await waitFor(() => expect(fake.calls.some((c) => c.path === "/api/projects")).toBe(true));
    expect(screen.queryByRole("option", { name: "New chat" })).toBeNull();
    expect(screen.queryByRole("option", { name: "Explorer" })).toBeNull();
  });

  it("g c and g e go nowhere on a linked project, and work on a normal one", async () => {
    const linked = mount("/p/linked/scans");
    await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() => expect(fake.calls.some((c) => c.path === "/api/projects/linked")).toBe(true));
    const user = userEvent.setup();
    await user.keyboard("gc");
    await user.keyboard("ge");
    expect(linked.state.location.pathname).toBe("/p/linked/scans");
    document.body.innerHTML = "";

    const normal = mount("/p/alpha/scans");
    await chats();
    await user.keyboard("gc");
    await waitFor(() => expect(normal.state.location.pathname).toBe("/p/alpha/chat"));
    await user.keyboard("ge");
    await waitFor(() => expect(normal.state.location.pathname).toBe("/p/alpha/explorer"));
  });

  it("g c goes nowhere for a viewer", async () => {
    fake.projects.alpha = project("alpha", { my_role: "viewer", permissions: ["project.read"] });
    const router = mount("/p/alpha/scans");
    await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() => expect(fake.calls.some((c) => c.path === "/api/projects/alpha")).toBe(true));
    await userEvent.setup().keyboard("gc");
    expect(router.state.location.pathname).toBe("/p/alpha/scans");
    expect(sessionCalls()).toHaveLength(0);
  });
});
