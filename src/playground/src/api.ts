// Typed client for the WhyGraph Explorer API. Shapes mirror `serve/routes.py`
// exactly — the same payloads the MCP tools serve, over HTTP.

export interface Symbol {
  id: string;
  qualified_name: string;
  name: string;
  kind: string;
  file_path: string;
  start_line: number;
  end_line: number;
  signature: string | null;
}

export interface SearchResult extends Symbol {
  analyzed: boolean;
}

export interface TreeEntry {
  id: string;
  label: string;
  kind: string; // "directory" | file/class/method/…
  has_children: boolean;
  node_id?: string;
  qualified_name?: string;
  path?: string;
  dir?: string;
}

export interface RelationSymbol extends Symbol {
  edge_kind?: string;
  edge_line?: number | null;
}

export interface NodeRelations {
  callers: RelationSymbol[];
  callees: RelationSymbol[];
  imports: RelationSymbol[];
  container: Symbol | null;
  children: Symbol[];
}

export interface NodeDetail {
  symbol: Symbol;
  analyzed: boolean;
  relations: NodeRelations;
}

export interface EgoNode {
  id: string;
  position: { x: number; y: number };
  data: Symbol & { is_focus: boolean };
}

export interface EgoEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
}

export interface EgoGraph {
  focus: string;
  nodes: EgoNode[];
  edges: EgoEdge[];
}

export interface OverviewNodeDto {
  id: string;
  kind: "directory" | "file";
  label: string;
  path: string;
  coverage: { analyzed: number; total: number; fraction: number };
  internal_edges: number;
}

export interface OverviewEdgeDto {
  id: string;
  source: string;
  target: string;
  kind: string;
  weight: number;
}

export interface OverviewGraph {
  expanded: string[];
  nodes: OverviewNodeDto[];
  edges: OverviewEdgeDto[];
}

export interface RationaleCard {
  status: "cached" | "not_generated" | "no_evidence";
  target?: { path: string; line_start: number; line_end: number };
  purpose?: string;
  why?: string;
  constraints?: string[];
  tradeoffs?: string[];
  risks?: string[];
  model?: string;
  provider?: string;
  cached_at?: string;
  evidence_count?: { commits: number; prs: number; issues: number };
}

export interface CommitDict {
  sha: string;
  subject: string;
  body: string | null;
  llm_description: string | null;
  author_name: string;
  author_email: string;
  authored_at: string;
  committed_at: string;
}

export interface PullRequestDict {
  number: number;
  title: string;
  html_url: string | null;
  state: string;
}

export interface IssueDict {
  number: number;
  title: string;
  html_url: string | null;
  state: string;
}

export interface EvidenceItem {
  commit: CommitDict;
  pull_requests: PullRequestDict[];
  issues: IssueDict[];
  source: string;
}

export interface EvidenceResponse {
  target: unknown;
  evidence: EvidenceItem[];
}

export interface HistoryResponse {
  path: string;
  include_renames: boolean;
  evidence: EvidenceItem[];
}

// ---- chat (serve/chat.py) -------------------------------------------------

export interface ChatProvider {
  provider: string;
  configured: boolean;
  default_model: string;
  env_var: string | null;
}

export interface ChatModel {
  id: string;
  display_name: string;
}

export interface ChatModels {
  provider: string;
  // "live" = fetched from the provider; "fallback" = the built-in short list,
  // used when listing failed (a scoped key can chat but not enumerate).
  source: "live" | "fallback";
  default_model: string;
  models: ChatModel[];
  error?: string;
}

export interface ChatSession {
  id: number;
  title: string;
  provider: string;
  model: string;
  created_at: string;
  updated_at: string;
  message_count?: number;
}

export interface ChatToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_calls: ChatToolCall[];
  tool_call_id: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  // Which provider/model produced this row — assistant rows only. Recorded
  // per row because the model can be switched mid-conversation.
  provider: string | null;
  model: string | null;
  // Why this assistant turn failed, if it did — persisted so the banner
  // survives a refresh instead of living only in the SSE stream.
  error: string | null;
  created_at: string;
}

export interface ChatTranscript extends ChatSession {
  messages: ChatMessage[];
}

// The SSE frame union from serve/chat.py §7.2. `error` and `done` are both
// terminal — the UI must stop its spinner on either.
export type ChatEvent =
  | { type: "text_delta"; text: string }
  | { type: "tool_call"; id: string; name: string; arguments: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; result: string }
  | { type: "round_limit"; rounds: number }
  | {
      type: "done";
      message_id: number | null;
      input_tokens: number | null;
      output_tokens: number | null;
      finish_reason: string | null;
    }
  | { type: "error"; message: string };

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  return parse<T>(await fetch(`/api${path}`));
}

async function post<T>(path: string): Promise<T> {
  return parse<T>(await fetch(`/api${path}`, { method: "POST" }));
}

// Body-carrying variants. The Explorer endpoints take everything in the query
// string; the chat endpoints take JSON bodies.
async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  return parse<T>(
    await fetch(`/api${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

// Turn a Response into JSON, with clear errors. A non-JSON body on a 200 (e.g. an
// unmatched /api route falling through to the SPA's index.html) becomes a plain
// ApiError instead of a cryptic "did not match the expected pattern" JSON crash.
async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? body.error ?? res.statusText);
  }
  if (!res.headers.get("content-type")?.includes("application/json")) {
    throw new ApiError(res.status, "unexpected non-JSON response from the API");
  }
  return res.json() as Promise<T>;
}

const q = (qn: string) => encodeURIComponent(qn);

export const api = {
  search: (query: string, limit = 20) =>
    get<{ query: string; results: SearchResult[] }>(
      `/search?q=${encodeURIComponent(query)}&limit=${limit}`,
    ),
  tree: (opts: { dir?: string; node?: string } = {}) => {
    const params = new URLSearchParams();
    if (opts.dir) params.set("dir", opts.dir);
    if (opts.node) params.set("node", opts.node);
    const qs = params.toString();
    return get<{ entries: TreeEntry[] }>(`/tree${qs ? `?${qs}` : ""}`);
  },
  overview: (expanded = "") =>
    get<OverviewGraph>(`/graph/overview?expanded=${encodeURIComponent(expanded)}`),
  ego: (qualified_name: string) =>
    get<EgoGraph>(`/graph/ego?qualified_name=${q(qualified_name)}`),
  // qualified_name goes in the query string (a file node's qn is a path with
  // slashes; a path segment would break routing — see serve/routes.py).
  node: (qualified_name: string) => get<NodeDetail>(`/node?qualified_name=${q(qualified_name)}`),
  rationaleRead: (qualified_name: string) =>
    get<RationaleCard>(`/node/rationale?qualified_name=${q(qualified_name)}`),
  rationaleGenerate: (qualified_name: string) =>
    post<RationaleCard>(`/node/rationale?qualified_name=${q(qualified_name)}`),
  evidence: (qualified_name: string, limit = 20) =>
    get<EvidenceResponse>(`/node/evidence?qualified_name=${q(qualified_name)}&limit=${limit}`),
  history: (path: string, limit = 20) =>
    get<HistoryResponse>(`/history?path=${encodeURIComponent(path)}&limit=${limit}`),

  // ---- chat -----------------------------------------------------------------
  chatProviders: () => get<ChatProvider[]>("/chat/providers"),
  chatModels: (provider: string) =>
    get<ChatModels>(`/chat/models?provider=${encodeURIComponent(provider)}`),
  chatSessions: () => get<ChatSession[]>("/chat/sessions"),
  chatCreateSession: (body: { provider?: string; model?: string; title?: string }) =>
    send<ChatSession>("POST", "/chat/sessions", body),
  chatTranscript: (id: number) => get<ChatTranscript>(`/chat/sessions/${id}`),
  chatUpdateSession: (
    id: number,
    body: { title?: string; provider?: string; model?: string },
  ) => send<ChatSession>("PATCH", `/chat/sessions/${id}`, body),
  chatDeleteSession: async (id: number): Promise<void> => {
    const res = await fetch(`/api/chat/sessions/${id}`, { method: "DELETE" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new ApiError(res.status, body.detail ?? body.error ?? res.statusText);
    }
  },
};

/**
 * POST a chat message and consume the SSE response, calling `onEvent` per frame.
 *
 * Hand-rolled rather than `EventSource`, which is GET-only and so cannot carry
 * the message body — and adding a dependency for ~20 lines of framing isn't
 * worth it. Must be called from a user action, never an effect: StrictMode
 * double-invokes effects in dev, which would send the turn twice.
 *
 * `signal` powers the Stop button. Aborting mid-stream leaves the server's
 * generator to persist whatever it has (GeneratorExit) — the transcript stays
 * consistent, so the caller can simply refetch it.
 *
 * Resolves when the stream ends. Rejects on a *transport* failure; a provider
 * failure arrives as an in-band `{type: "error"}` frame instead, because the
 * HTTP status was already committed before the first token.
 */
export async function streamChat(
  sessionId: number,
  content: string,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? body.error ?? res.statusText);
  }
  if (!res.body) throw new ApiError(res.status, "streaming is unsupported here");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // Frames are `data: <json>\n\n`. A chunk can split a frame anywhere, so keep
  // the trailing partial in the buffer until its terminator arrives.
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const line = block.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as ChatEvent);
      } catch {
        // A truncated final frame (server killed mid-write) is not worth
        // failing the whole turn over — the UI already has everything before it.
      }
    }
  }
}

export { ApiError };
