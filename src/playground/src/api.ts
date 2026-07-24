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

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? body.error ?? res.statusText);
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? body.error ?? res.statusText);
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
  node: (qualified_name: string) => get<NodeDetail>(`/node/${q(qualified_name)}`),
  rationaleRead: (qualified_name: string) =>
    get<RationaleCard>(`/node/${q(qualified_name)}/rationale`),
  rationaleGenerate: (qualified_name: string) =>
    post<RationaleCard>(`/node/${q(qualified_name)}/rationale`),
  evidence: (qualified_name: string, limit = 20) =>
    get<EvidenceResponse>(`/node/${q(qualified_name)}/evidence?limit=${limit}`),
  history: (path: string, limit = 20) =>
    get<HistoryResponse>(`/history?path=${encodeURIComponent(path)}&limit=${limit}`),
};

export { ApiError };
