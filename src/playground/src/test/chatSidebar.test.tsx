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
  effort?: string | null;
}

interface Fake {
  projects: Record<string, Record<string, unknown>>;
  sessions: Session[];
  calls: { path: string; method: string; body: unknown; signal?: AbortSignal | null }[];
  /** The open SSE stream of the last message POST, fed by the test. */
  stream: ReadableStreamDefaultController<Uint8Array> | null;
  /** Messages the transcript returns, per session. */
  transcripts: Record<number, unknown[]>;
  /** Extra top-level fields of a session's transcript (`versions`, `streaming`, ...), per session. */
  transcriptExtra: Record<number, Record<string, unknown>>;
  /** A turn holds the session's lease: turn routes answer 409 until `gets` transcript reads have passed. */
  lease: { gets: number } | null;
  /** What `POST .../branch` answers with (a transcript's messages + extra), per session. */
  branched: Record<number, { messages: unknown[]; extra: Record<string, unknown> }>;
  /** The provider list the project answers (default: one keyed OpenAI). */
  providers?: {
    provider: string;
    configured: boolean;
    default_model: string;
    default: boolean;
    default_effort?: string | null;
  }[];
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
      return Promise.resolve(
        json(fake.providers ?? [{ provider: "openai", configured: true, default_model: "gpt-x", default: true }]),
      );
    }
    if (rest === "/chat/models") {
      return Promise.resolve(
        json({ provider: "openai", source: "live", default_model: "gpt-x", models: [{ id: "gpt-x", display_name: "GPT X" }] }),
      );
    }
    if (rest === "/scan-estimate" && slug === "unsafe") {
      return Promise.resolve(json({ error: "a symlink is in the way", code: "unsafe_path", path: ".whygraph" }, 409));
    }
    if (rest === "/chat/sessions" && method === "GET") return Promise.resolve(json(fake.sessions));
    if (rest === "/chat/sessions" && method === "POST") {
      const b = (body ?? {}) as { provider?: string; model?: string; effort?: string };
      const row = {
        ...session(7, "New chat", 0),
        provider: b.provider ?? "openai",
        model: b.model ?? "gpt-x",
        message_count: 0,
        effort: b.effort ?? null,
      };
      fake.sessions = [row, ...fake.sessions];
      return Promise.resolve(json(row, 201));
    }
    // Edit / regenerate / continue stream like a send; branch answers with the transcript of the new path.
    const t = /^\/chat\/sessions\/(\d+)\/messages\/(\d+)\/(edit|regenerate|continue)$/.exec(rest ?? "");
    if (t && method === "POST") return Promise.resolve(turnStream(init));
    const br = /^\/chat\/sessions\/(\d+)\/branch$/.exec(rest ?? "");
    if (br && method === "POST") {
      const id = Number(br[1]);
      const next = fake.branched[id];
      const row = fake.sessions.find((x) => x.id === id) ?? session(id, "t");
      return Promise.resolve(json({ ...row, messages: next.messages, ...next.extra }));
    }
    const s = /^\/chat\/sessions\/(\d+)(\/messages)?$/.exec(rest ?? "");
    if (s) {
      const id = Number(s[1]);
      if (s[2] && method === "POST") {
        // The server titles a "New chat" from its first message before the first frame.
        const content = (body as { content: string }).content;
        fake.sessions = fake.sessions.map((x) => (x.id === id && x.title === "New chat" ? { ...x, title: content } : x));
        return Promise.resolve(turnStream(init));
      }
      if (method === "PATCH") {
        const patch = body as { title?: string; effort?: string | null };
        const title = patch.title;
        fake.sessions = fake.sessions.map((x) => {
          if (x.id !== id) return x;
          // An explicit null resets the level; an absent key leaves it alone (the real server tells them apart).
          const next = "effort" in patch ? { ...x, effort: patch.effort } : x;
          return title ? { ...next, title } : next;
        });
        return Promise.resolve(json(fake.sessions.find((x) => x.id === id)));
      }
      if (method === "DELETE") {
        fake.sessions = fake.sessions.filter((x) => x.id !== id);
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      const row = fake.sessions.find((x) => x.id === id) ?? session(id, "t");
      // Each read counts down the lease: the stopped turn lets go of the session after `gets` of them.
      const leased = !!fake.lease && --fake.lease.gets > 0;
      if (fake.lease && !leased) fake.lease = null;
      return Promise.resolve(
        json({ ...row, messages: fake.transcripts[id] ?? [], streaming: leased, ...fake.transcriptExtra[id] }),
      );
    }
  }
  return Promise.resolve(json({ error: "unhandled" }, 500));
}

/** A turn's SSE answer (or the 409 of a busy session). Aborting errors the stream, as a real fetch does. */
function turnStream(init?: RequestInit): Response {
  if (fake.lease) {
    return json({ error: "A turn is already running in this chat.", code: "turn_in_progress" }, 409);
  }
  return new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        fake.stream = c;
        init?.signal?.addEventListener("abort", () => c.error(new DOMException("Aborted", "AbortError")));
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
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
      legacy: project("legacy", { root_status: "missing", stats: null }),
      notgit: project("notgit", { root_status: "not_git", stats: null }),
    },
    sessions: [],
    calls: [],
    stream: null,
    transcripts: {},
    transcriptExtra: {},
    lease: null,
    branched: {},
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

  it.each(["legacy", "notgit"])(
    "is absent, with no sessions request, when the project folder cannot be opened (%s)",
    async (slug) => {
      mount(`/p/${slug}/settings`);
      const nav = await screen.findByRole("navigation", { name: "Main" });
      await waitFor(() => expect(fake.calls.some((c) => c.path === `/api/projects/${slug}`)).toBe(true));
      await within(nav).findByRole("link", { name: "Settings" });
      expect(screen.queryByTestId("chats-section")).toBeNull();
      expect(sessionCalls()).toHaveLength(0);
    },
  );

  it("goes away once the probe finds a symlink in the way", async () => {
    fake.projects.unsafe = project("unsafe", { stats: null });
    mount("/p/unsafe/explorer");
    expect(await screen.findByTestId("problem-unsafe_path")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("chats-section")).toBeNull());
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

  it("disables + with a reason when no provider has a key", async () => {
    fake.providers = [{ provider: "openai", configured: false, default_model: "gpt-x", default: false }];
    mount("/p/alpha/scans");
    const section = await chats();
    const plus = within(section).getByRole("button", { name: "New chat" });
    await waitFor(() => expect(plus).toBeDisabled());
    const reason = document.getElementById(plus.getAttribute("aria-describedby") ?? "");
    expect(reason).toHaveTextContent("No LLM key is set for Chat");
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

// Edit, regenerate and the version arrows (chat harness v2, step 7a), on a session with two exchanges.
describe("the chat view: edit, regenerate and versions", () => {
  const row = (id: number, role: string, content: string, over: Record<string, unknown> = {}) => ({
    id,
    role,
    content,
    tool_calls: [],
    tool_call_id: null,
    input_tokens: null,
    output_tokens: null,
    provider: "openai",
    model: "gpt-x",
    error: null,
    created_at: "",
    ...over,
  });
  const exchanges = () => [
    row(1, "user", "first q"),
    row(2, "assistant", "first a"),
    row(3, "user", "second q"),
    row(4, "assistant", "second a"),
  ];
  const turnPosts = () =>
    fake.calls.filter((c) => c.method === "POST" && /\/chat\/sessions\/3\/(messages|branch)/.test(c.path));
  const done = (id: number) => frame({ type: "done", message_id: id, input_tokens: 1, output_tokens: 1, finish_reason: "stop" });

  beforeEach(() => {
    fake.sessions = [session(3, "Two exchanges")];
    fake.transcripts[3] = exchanges();
  });

  it("offers Regenerate on the last reply only, and Edit on every question", async () => {
    mount("/p/alpha/chat/3");
    await screen.findByText("second a");
    expect(screen.getAllByRole("button", { name: "Regenerate reply" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Edit message" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Copy reply" })).toHaveLength(2);
  });

  it("edits a question: the edit route gets the content, later turns give way, the refetch brings the version", async () => {
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    await screen.findByText("second a");
    await user.click(screen.getAllByRole("button", { name: "Edit message" })[1]);
    const box = screen.getByRole("textbox", { name: "Edit message" });
    await user.clear(box);
    await user.type(box, "second q, better");
    await user.keyboard("{Enter}");

    await waitFor(() => expect(fake.stream).not.toBeNull());
    expect(turnPosts().at(-1)).toMatchObject({
      path: "/api/projects/alpha/chat/sessions/3/messages/3/edit",
      body: { content: "second q, better" },
    });
    // The question and everything after it are replaced by the live view; the earlier exchange stays.
    expect(screen.getByText("first a")).toBeInTheDocument();
    expect(screen.queryByText("second a")).toBeNull();
    expect(screen.getByText("second q, better")).toBeInTheDocument();
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();
    // Nothing else can be started meanwhile.
    expect(screen.queryByRole("button", { name: "Edit message" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Regenerate reply" })).toBeNull();

    act(() => fake.stream!.enqueue(frame({ type: "text_delta", text: "A better " })));
    await screen.findByText(/A better/);
    fake.transcripts[3] = [
      row(1, "user", "first q"),
      row(2, "assistant", "first a"),
      row(5, "user", "second q, better"),
      row(6, "assistant", "A better answer."),
    ];
    fake.transcriptExtra[3] = { versions: { "5": { index: 2, count: 2, siblings: [3, 5] } } };
    act(() => {
      fake.stream!.enqueue(frame({ type: "text_delta", text: "answer." }));
      fake.stream!.enqueue(done(6));
      fake.stream!.close();
    });
    await screen.findByText("A better answer.");
    expect(screen.getByTestId("version-arrows")).toHaveTextContent("2/2");
  });

  it("regenerates the last reply: the regenerate route, no body, the old reply gives way", async () => {
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    await screen.findByText("second a");
    await user.click(screen.getByRole("button", { name: "Regenerate reply" }));

    await waitFor(() => expect(fake.stream).not.toBeNull());
    expect(turnPosts().at(-1)).toMatchObject({
      path: "/api/projects/alpha/chat/sessions/3/messages/3/regenerate",
      body: undefined,
    });
    expect(screen.queryByText("second a")).toBeNull();
    expect(screen.getByText("second q")).toBeInTheDocument();
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();

    fake.transcripts[3] = [...exchanges().slice(0, 3), row(7, "assistant", "Second try.")];
    fake.transcriptExtra[3] = { versions: { "7": { index: 2, count: 2, siblings: [4, 7] } } };
    act(() => {
      fake.stream!.enqueue(frame({ type: "text_delta", text: "Second try." }));
      fake.stream!.enqueue(done(7));
      fake.stream!.close();
    });
    await screen.findByText("Second try.");
    expect(screen.getByTestId("version-arrows")).toHaveTextContent("2/2");
  });

  it("continues a reply the output limit cut off: the continue route, no body, the reply stays and merges on refetch", async () => {
    const cut = () => [...exchanges().slice(0, 3), row(4, "assistant", "second a, cut", { finish_reason: "max_tokens" })];
    fake.transcripts[3] = cut();
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    await screen.findByText("second a, cut");
    expect(screen.getAllByTestId("output-limit-notice")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(fake.stream).not.toBeNull());
    expect(turnPosts().at(-1)).toMatchObject({
      path: "/api/projects/alpha/chat/sessions/3/messages/4/continue",
      body: undefined,
    });
    // No new question; the truncated reply stays while the rest streams in, and nothing else can start.
    expect(screen.getByText("second a, cut")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Regenerate reply" })).toBeNull();

    act(() => fake.stream!.enqueue(frame({ type: "text_delta", text: " and the rest." })));
    await screen.findByText(/and the rest\./);
    fake.transcripts[3] = [...cut(), row(5, "assistant", " and the rest.", { finish_reason: "stop" })];
    act(() => {
      fake.stream!.enqueue(done(5));
      fake.stream!.close();
    });
    // The two rows are one reply now, with no output-limit notice and nothing left to continue.
    await screen.findByText("second a, cut and the rest.");
    expect(screen.queryByTestId("output-limit-notice")).toBeNull();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
  });

  it("offers Continue on the last reply only, and not on a reply that ended normally", async () => {
    // An older cut-off reply is no longer the leaf; the last reply ended normally.
    fake.transcripts[3] = [
      row(1, "user", "first q"),
      row(2, "assistant", "first a, cut", { finish_reason: "length" }),
      row(3, "user", "second q"),
      row(4, "assistant", "second a", { finish_reason: "stop" }),
    ];
    mount("/p/alpha/chat/3");
    await screen.findByText("second a");
    expect(screen.getByTestId("output-limit-notice")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
  });

  it.each([
    ["edit", async (user: ReturnType<typeof userEvent.setup>) => {
      await user.click(screen.getAllByRole("button", { name: "Edit message" })[1]);
      await user.type(screen.getByRole("textbox", { name: "Edit message" }), "!{Enter}");
    }],
    ["regenerate", async (user: ReturnType<typeof userEvent.setup>) => {
      await user.click(screen.getByRole("button", { name: "Regenerate reply" }));
    }],
  ])("Stop during %s aborts the stream and ends the turn", async (_name, start) => {
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    await screen.findByText("second a");
    await start(user);
    await waitFor(() => expect(fake.stream).not.toBeNull());
    const post = turnPosts().at(-1);
    expect(post?.signal?.aborted).toBe(false);
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(post?.signal?.aborted).toBe(true);
    // The turn ends and the refetch brings the persisted transcript back (the real server writes a
    // "Stopped." row; the fake has none): the thread is whole again and the composer usable.
    await screen.findByRole("button", { name: "Send" });
    await screen.findByText("second a");
    expect(screen.getByText("second q")).toBeInTheDocument();
    expect(screen.queryByText(/Thinking/)).toBeNull();
  });

  it("a 409 right after the person's own Stop says so, retries on its own, then sends", async () => {
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox");
    await screen.findByText("second a");
    await user.type(box, "one");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fake.stream).not.toBeNull());
    await user.click(screen.getByRole("button", { name: "Stop" }));
    await screen.findByRole("button", { name: "Send" });

    // The stopped turn is still finishing a tool call: the session stays leased for two reads.
    fake.lease = { gets: 2 };
    fake.stream = null;
    await user.type(screen.getByRole("textbox"), "two");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Stopping the previous reply…");
    // No error is shown while it waits.
    expect(screen.queryByText(/still being written/)).toBeNull();
    await waitFor(() => expect(fake.stream).not.toBeNull(), { timeout: 4000 });
    expect(turnPosts().filter((c) => c.path.endsWith("/messages"))).toHaveLength(3);
    expect(screen.queryByText("Stopping the previous reply…")).toBeNull();
    act(() => {
      fake.stream!.enqueue(frame({ type: "text_delta", text: "ok" }));
      fake.stream!.enqueue(done(9));
      fake.stream!.close();
    });
    await screen.findByRole("button", { name: "Send" });
  }, 15_000);

  it("a 409 with no Stop before it is an ordinary error", async () => {
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox");
    await screen.findByText("second a");
    fake.lease = { gets: 99 };
    await user.type(box, "hello");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/still being written in this chat/);
    expect(screen.queryByText("Stopping the previous reply…")).toBeNull();
  });

  it("an edit turned away before it wrote anything puts the thread back and says why", async () => {
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    await screen.findByText("second a");
    fake.lease = { gets: 99 };
    await user.click(screen.getAllByRole("button", { name: "Edit message" })[1]);
    await user.type(screen.getByRole("textbox", { name: "Edit message" }), "!{Enter}");
    await screen.findByText(/still being written in this chat/);
    expect(screen.getByText("second a")).toBeInTheDocument();
    expect(screen.getByText("second q")).toBeInTheDocument();
  });

  it("the version arrows branch, then show the path the server answers with", async () => {
    fake.transcripts[3] = [row(1, "user", "first q"), row(4, "assistant", "second a")];
    fake.transcriptExtra[3] = { versions: { "1": { index: 2, count: 3, siblings: [1, 5, 9] } } };
    fake.branched[3] = {
      messages: [row(1, "user", "first q"), row(6, "assistant", "the other answer")],
      extra: { versions: { "1": { index: 3, count: 3, siblings: [1, 5, 9] } } },
    };
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    await screen.findByText("second a");
    expect(screen.getByTestId("version-arrows")).toHaveTextContent("2/3");
    await user.click(screen.getByRole("button", { name: "Next version" }));
    await screen.findByText("the other answer");
    expect(turnPosts().at(-1)).toMatchObject({
      path: "/api/projects/alpha/chat/sessions/3/branch",
      body: { message_id: 9 },
    });
    expect(screen.getByTestId("version-arrows")).toHaveTextContent("3/3");
    expect(screen.getByRole("button", { name: "Next version" })).toBeDisabled();
  });
});

// The effort select beside the model pickers (chat harness v2, step 7b).
describe("the chat view: effort", () => {
  const providers = (default_effort: string | null) => [
    { provider: "openai", configured: true, default_model: "gpt-x", default: true, default_effort },
  ];
  const patches = () => fake.calls.filter((c) => c.method === "PATCH" && c.path === "/api/projects/alpha/chat/sessions/3");
  const pick = async (user: ReturnType<typeof userEvent.setup>, option: string | RegExp) => {
    await user.click(await screen.findByRole("combobox", { name: "Effort" }));
    await user.click(await screen.findByRole("option", { name: option }));
  };

  it("labels Default with the inherited level, or with the provider default when none is set", async () => {
    fake.providers = providers("medium");
    fake.sessions = [session(3, "Chat")];
    mount("/p/alpha/chat/3");
    expect(await screen.findByRole("combobox", { name: "Effort" })).toHaveTextContent("Default (Medium)");
    expect(screen.getByRole("combobox", { name: "Effort" })).toHaveAttribute(
      "title",
      "How hard the model thinks. Default uses the level set in Settings.",
    );
  });

  it("says provider default when nothing is set", async () => {
    fake.providers = providers(null);
    fake.sessions = [session(3, "Chat")];
    mount("/p/alpha/chat/3");
    expect(await screen.findByRole("combobox", { name: "Effort" })).toHaveTextContent("Default (provider default)");
  });

  it("PATCHes the level on an existing session, and null for Default", async () => {
    fake.providers = providers("low");
    fake.sessions = [session(3, "Chat")];
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    await pick(user, "High");
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0].body).toEqual({ effort: "high" });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Effort" })).toHaveTextContent("High"));

    await pick(user, /^Default/);
    await waitFor(() => expect(patches()).toHaveLength(2));
    expect(patches()[1].body).toEqual({ effort: null });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Effort" })).toHaveTextContent("Default (Low)"));
  });

  it("keeps a draft choice local and sends it in the create body on the first message", async () => {
    mount("/p/alpha/chat");
    const user = userEvent.setup();
    await pick(user, "Extra high");
    expect(fake.calls.some((c) => c.method === "POST" || c.method === "PATCH")).toBe(false);
    await user.type(await screen.findByRole("textbox"), "why?");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fake.stream).not.toBeNull());
    const creates = fake.calls.filter((c) => c.method === "POST" && c.path === "/api/projects/alpha/chat/sessions");
    expect(creates[0].body).toEqual({ provider: "openai", model: "gpt-x", effort: "xhigh" });
    // The new session keeps the level the draft had.
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Effort" })).toHaveTextContent("Extra high"));
  });

  it("is disabled while a turn streams", async () => {
    fake.sessions = [session(3, "Chat")];
    mount("/p/alpha/chat/3");
    const user = userEvent.setup();
    expect(await screen.findByRole("combobox", { name: "Effort" })).toBeEnabled();
    await user.type(await screen.findByRole("textbox"), "go");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fake.stream).not.toBeNull());
    expect(screen.getByRole("combobox", { name: "Effort" })).toBeDisabled();
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
