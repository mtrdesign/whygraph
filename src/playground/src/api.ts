// Typed client for the WhyGraph Explorer API. Shapes mirror `serve/routes.py`
// exactly — the same payloads the MCP tools serve, over HTTP.

import { hardNavigate } from "./lib/navigation";

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

// ---- portal (portal/routes.py) --------------------------------------------

export interface PortalUser {
  uid: string;
  display_name: string;
  role: string | null;
  // Production only (M2c). `email` is null for a GitHub account (M2d-1).
  email?: string | null;
  is_instance_admin?: boolean;
  // Production only (M2d-1).
  github_login?: string | null;
  avatar_url?: string | null;
  has_password?: boolean;
}

// `GET /api/portal/state` is public. In degraded mode (the portal DB failed to
// migrate) the body is just `{ error }`.
export interface PortalState {
  mode?: string;
  setup_complete?: boolean;
  user?: PortalUser | null;
  port?: number;
  shared_folders?: string[];
  // `null` only when the package metadata is missing.
  version?: string | null;
  // What the portal did when it started on a new port (computed once at start).
  port_change?: PortChange | null;
  // The organization the request is in (M2b); null before setup. `reader` is an
  // instance admin looking at an org they are not a member of (read-only).
  org?: PortalOrg | null;
  // Production only (M2c); a missing `host_kind` means local mode.
  host_kind?: "local" | "base" | "org";
  base_url?: string;
  bootstrap_required?: boolean;
  error?: string;
}

export interface PortalOrg {
  slug: string;
  name: string;
  role: "owner" | "admin" | "member" | "reader" | (string & {});
}

// ---- port change (portal/port_change.py) ----------------------------------------

export interface PortChangeAgent {
  agent: string;
  file: string;
  // env: nothing rewritten, `hint` says what to do; manual: tracked or unparseable,
  // `line` is the entry to set (plus `diff` / `snippet`); skipped: see `reason`.
  action: "rewritten" | "up_to_date" | "env" | "manual" | "skipped";
  hint?: string;
  line?: string;
  diff?: string | null;
  snippet?: string | null;
  reason?: string | null;
}

export interface PortChangeProject {
  slug: string;
  root: string;
  previous_port: number;
  markers: "rewritten" | "skipped";
  reason?: string;
  agents: PortChangeAgent[];
}

export interface PortChange {
  port: number;
  previous_port: number | null;
  projects: PortChangeProject[];
  // Initialized projects whose folder was not available at start.
  unmounted: { slug: string; root: string }[];
}

/** `GET /api/projects/{slug}`'s `port_change`: this project's item, or unmounted. */
export type ProjectPortChange =
  | PortChangeProject
  | { slug: string; root: string; unmounted: true; port: number };

export interface ProjectSummary {
  slug: string;
  name: string;
  source: "local" | "github" | "platform";
  root: string;
  remote_url: string | null;
  initialized: boolean;
  initialized_at: string | null;
  last_scan_at: string | null;
  created_at: string;
  root_status: "ok" | "missing" | "not_git";
  running_scan: { id: number; status: string; trigger: string } | null;
  // Status of the newest *ended* run, or `null` when none has ended yet.
  last_scan_status?: "ok" | "failed" | "interrupted" | "cancelled" | null;
  // `null` = up to date / never scanned; `commits_behind` is null after a history rewrite.
  stale: { commits_behind: number | null } | null;
  // False for a local-mode GitHub clone of an older build: listed, not scannable, removable.
  source_supported: boolean;
  // A production project GitHub no longer lets WhyGraph read (scans are refused).
  access_lost: boolean;
  access_lost_reason: AccessLostReason | null;
  // An imported repo's `owner/name` and the account the app is installed on.
  github_full_name: string | null;
  installation_account: string | null;
  // A `platform` project's link to its platform project (local mode, M2e); absent otherwise.
  link?: ProjectLink | null;
}

/** How a linked project's connection to its platform stands (plan section 4.11). */
export type LinkStatus = "ok" | "access_lost" | "removed" | "revoked" | "unreachable" | "update_required";

/** `_summary`'s `link` of a `platform` project. The three URLs are built by the server. */
export interface ProjectLink {
  platform_origin: string;
  org: string;
  remote_slug: string;
  status: LinkStatus;
  status_reason: string | null;
  last_platform_head: string | null;
  explorer_url: string;
  chat_url: string;
  manage_url: string;
}

export type AccessLostReason = "no_access" | "git_access_denied" | "repo_deleted" | "tracked_whygraph_state";

export interface ProjectDetails extends ProjectSummary {
  agents: string[];
  missing_key: string | null;
  // `null` in production (no MCP endpoint there).
  mcp_url: string | null;
  // The repo's 1.x / agent-file state, recomputed on every read; `null` while
  // the project folder is unusable.
  detected?: Detected | null;
  port_change?: ProjectPortChange | null;
  stats: {
    commits: number;
    described: number;
    described_pct: number;
    pull_requests: number;
    issues: number;
    rationale_cards: number;
  } | null;
}

// ---- portal setup / add wizard ------------------------------------------------

export interface RepoEntry {
  path: string;
  name: string;
  registered: boolean;
}

export interface CheckPathResult {
  path: string;
  shared: boolean;
  is_git: boolean;
  protected: boolean;
  folder_suggestion: string | null;
  command: string | null;
  github: { slug: string; remote_url: string } | null;
}

export interface DetectedAgent {
  agent: string;
  file: string;
  key: string;
  shape: "stdio" | "http" | "unknown";
  stale: boolean;
  tracked: boolean;
}

export interface CustomDbPath {
  key: string;
  path: string;
  exists: boolean;
  message: string;
}

// The 1.x state a repo already carries; returned once, by `POST /api/projects`.
export interface Detected {
  existing_db: boolean;
  managed_hooks: string[];
  detected_agents: DetectedAgent[];
  custom_db_paths: CustomDbPath[];
}

export interface ImportReport {
  found: boolean;
  error: string | null;
  secrets_moved: string[];
  dropped: { key: string; hint: string }[];
  custom_db_paths: CustomDbPath[];
  warnings: string[];
}

export interface AddProjectResult {
  project: ProjectDetails;
  detected: Detected;
  import: ImportReport;
}

export type AddProjectBody =
  | { source: "local"; path: string; name?: string; token?: string }
  | { source: "github"; installation_id: number; repo_id: number; name?: string };

/** A v2 config layer (`[llm]`, `[analyze]`, ... as nested tables). */
export type ConfigDict = Record<string, unknown>;

export interface SecretStatus {
  set: boolean;
  hint: string | null;
  unreadable?: boolean;
}

export interface SecretsView {
  llm: Record<string, SecretStatus>;
  github_token: SecretStatus;
  /** A `claude setup-token` subscription token for the claude-cli provider. */
  claude_oauth_token?: SecretStatus;
}

/** Write-only: a string sets, `null` deletes, an absent key leaves it alone. */
export interface SecretsPatch {
  llm?: Record<string, string | null>;
  github_token?: string | null;
  claude_oauth_token?: string | null;
}

export interface ConfigPut {
  config?: ConfigDict;
  secrets?: SecretsPatch;
}

export interface ProjectConfigView {
  config: ConfigDict;
  secrets: SecretsView;
  import: ImportReport;
  // Present on a PUT response only.
  hooks?: unknown;
  hooks_error?: string | null;
}

export interface DefaultsView {
  config: ConfigDict;
  secrets: SecretsView;
  no_provider_key: boolean;
  // Present on a PUT response only: projects whose own key for a provider was
  // cleared because the global endpoint they inherit changed.
  cleared_project_keys?: { slug: string; provider: string }[];
}

export type FileStatus = "write" | "overwrite" | "skip" | "refused" | "needs_confirmation";

export interface FileOutcome {
  file: string;
  status: FileStatus;
  agent: string | null;
  reason: string | null;
  snippet: string | null;
  diff: string | null;
}

export interface InitBody {
  agents: string[];
  force?: boolean;
  dry_run?: boolean;
  confirm_tracked?: string[];
  agent_actions?: Record<string, "migrate" | "remove">;
}

export interface InitResult {
  dry_run: boolean;
  gitignore_added: string[];
  hooks: { installed: string[]; removed: string[]; actions: Record<string, string> } | null;
  hooks_error: string | null;
  agent_files: FileOutcome[];
  asset_files: FileOutcome[];
  configured_agents: string[];
  needs_confirmation: string[];
  refused: string[];
  marker_written: boolean;
  initialized: boolean;
  custom_db_paths: CustomDbPath[];
}

export interface ScanEstimate {
  commits: number;
  upper_bound: boolean;
  large_commits: number;
  model: { provider: string | null; model: string | null };
  tokens: {
    input: number;
    output: number;
    input_range: { low: number; high: number };
    output_range: { low: number; high: number };
  };
  cost: {
    usd: number;
    low: number;
    high: number;
    currency: string;
    prices_as_of: string;
  } | null;
  missing_key: string | null;
}

export type ScanRunStatus =
  | "queued"
  | "running"
  | "ok"
  | "failed"
  | "interrupted"
  | "cancelled";

/** The fields of a run's `summary` the UI reads (the backend may add more). */
export interface ScanRunSummary {
  status?: string;
  elapsed_sec?: number;
  phase_timings?: Record<string, number>;
  crawlers?: { name: string; status: string; summary?: string; error?: string; warning?: string }[];
  analyze_skipped?: string | null;
  exit_code?: number;
  moved?: boolean;
  error?: string;
  merged_into?: number;
  /** `"user"` when someone cancelled the run (vs. a merged or orphaned one). */
  cancelled_by?: string;
  [k: string]: unknown;
}

/** `GET .../scans/{id}/log`: the tail (at most 64 KiB) of a run's log. */
export interface ScanLog {
  run_id: number;
  text: string;
  size: number;
  truncated: boolean;
}

export interface DeleteProjectBody {
  strip_agent_entries?: boolean;
  confirm_tracked?: string[];
  confirm_name?: string;
}

export interface DeleteProjectResult {
  removed: string;
  hooks: unknown;
  agent_files: FileOutcome[];
  checkout_deleted: boolean;
  warnings: string[];
}

export interface ScanRunRow {
  id: number;
  kind: "scan" | "sync";
  trigger: string;
  analyze: boolean;
  status: ScanRunStatus;
  requested_by: number | null;
  started_at: string | null;
  finished_at: string | null;
  summary: ScanRunSummary | null;
}

// The scan events stream (`portal/runner.py`): the child's JSONL events plus the
// runner's own `sync` / `error` / `end` / `shutdown` frames.
export type ScanEvent =
  | { type: "start"; phase_total: number }
  | { type: "phase"; phase: number; title: string }
  | {
      type: "task";
      name: string;
      completed?: number;
      total?: number | null;
      description?: string;
    }
  | {
      type: "result";
      status: "ok" | "failed";
      elapsed_sec?: number;
      crawlers?: { name: string; status: string; summary?: string; error?: string }[];
      [k: string]: unknown;
    }
  | { type: "sync"; status: "fetching" | "ok" | "failed"; moved?: boolean; error?: string }
  | { type: "error"; message: string }
  | { type: "end"; run_id: number; status: ScanRunStatus; summary: ScanRunSummary | null }
  /** The stream was cut because the viewer lost access (signed out, removed, disabled); the run goes on. */
  | { type: "end"; run_id: number; status: null; summary: null; reason: "access_revoked" }
  | { type: "shutdown"; run_id: number };

// ---- transport --------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    // The portal's machine-readable `code` (`setup_required`, `not_found`, ...).
    public code?: string,
    // Extra top-level fields of the error body (`command`, `folder_suggestion`, `keys`, ...).
    public extra: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

// Every request - GETs included - carries this header. The portal refuses a
// request without it (403): a cross-origin page cannot add a custom header
// without a CORS preflight, which is the CSRF guard (`portal/security.py`).
const CLIENT_HEADERS = { "X-WhyGraph-Client": "1" } as const;

function init(method: string, body?: unknown, signal?: AbortSignal): RequestInit {
  const headers: Record<string, string> = { ...CLIENT_HEADERS };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  };
}

// The public base URL, set by the root gate from the portal state (production only).
let baseUrl: string | null = null;

/** Tell the transport where the sign-in page lives; `null` in local mode. */
export function setBaseUrl(url: string | null): void {
  baseUrl = url;
}

// An org host without a session answers `401 login_required`: send the browser to
// the base host's sign-in with a way back. `bad_credentials` (also 401) never gets
// here, and the base host's own /signin never redirects to itself.
function redirectToSignIn(): void {
  if (!baseUrl) return;
  let base: URL;
  try {
    base = new URL(baseUrl);
  } catch {
    return;
  }
  if (window.location.origin === base.origin && window.location.pathname === "/signin") return;
  hardNavigate(`${base.origin}/signin?next=${encodeURIComponent(window.location.href)}`);
}

async function failure(res: Response): Promise<ApiError> {
  const body = await res.json().catch(() => ({}));
  const { detail, error, code, ...extra } = body;
  if (res.status === 401 && code === "login_required") redirectToSignIn();
  return new ApiError(res.status, detail ?? error ?? res.statusText, code, extra);
}

// Turn a Response into JSON, with clear errors. A non-JSON body on a 200 (e.g. an
// unmatched route falling through to the SPA's index.html) becomes a plain
// ApiError instead of a cryptic "did not match the expected pattern" JSON crash.
async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) throw await failure(res);
  if (!res.headers.get("content-type")?.includes("application/json")) {
    throw new ApiError(res.status, "unexpected non-JSON response from the API");
  }
  return res.json() as Promise<T>;
}

// The Explorer endpoints take everything in the query string; the chat and
// portal endpoints take JSON bodies.
async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  return parse<T>(await fetch(`/api${path}`, init(method, body)));
}

const get = <T>(path: string) => send<T>("GET", path);

const q = (qn: string) => encodeURIComponent(qn);

// ---- portal-level calls -----------------------------------------------------

export const portalApi = {
  state: () => get<PortalState>("/portal/state"),
  projects: () => get<{ projects: ProjectSummary[] }>("/projects"),
  project: (slug: string) => get<ProjectDetails>(`/projects/${encodeURIComponent(slug)}`),
  setup: (display_name: string) =>
    send<{ setup_complete: boolean; user: PortalUser }>("POST", "/portal/setup", { display_name }),
  repos: (query = "") =>
    get<{ repos: RepoEntry[]; truncated: boolean }>(`/portal/repos?q=${encodeURIComponent(query)}`),
  checkPath: (path: string) => send<CheckPathResult>("POST", "/portal/check-path", { path }),
  defaults: () => get<DefaultsView>("/portal/defaults"),
  putDefaults: (body: ConfigPut) => send<DefaultsView>("PUT", "/portal/defaults", body),
  addProject: (body: AddProjectBody) => send<AddProjectResult>("POST", "/projects", body),
};

// ---- identity (portal/auth_routes.py, production only) ----------------------------

export interface Redirect {
  redirect: string;
}

export interface AccountView {
  uid: string;
  // Null for a GitHub account.
  email: string | null;
  display_name: string;
  is_instance_admin: boolean;
  github_login: string | null;
  avatar_url: string | null;
  has_password: boolean;
}

export interface OrgEntry {
  slug: string;
  name: string;
  role: string;
  url: string;
}

export interface AdminUser {
  uid: string;
  // Null for a GitHub account.
  email: string | null;
  display_name: string;
  is_instance_admin: boolean;
  github_login: string | null;
  has_password: boolean;
  disabled: boolean;
  created_at: string;
  org_count: number;
}

export type MemberRole = "owner" | "admin" | "member";

// One org member (`portal/member_routes.py`); never an email.
export interface Member {
  uid: string;
  display_name: string;
  github_login: string | null;
  avatar_url: string | null;
  role: MemberRole;
  joined_at: string;
  disabled: boolean;
}

export interface AdminOrg {
  slug: string;
  name: string;
  url: string;
  member_count: number;
  created_at: string;
}

export interface AdminSettings {
  base_url: string;
  // The base-URL self-check's warnings; `null` until it has run, `[]` when clean.
  base_check: string[] | null;
}

export const authApi = {
  bootstrap: (body: { secret: string; email: string; display_name: string; password: string }) =>
    send<Redirect>("POST", "/auth/bootstrap", body),
  // GitHub sign-in (M2d-1): `start` returns GitHub's authorize URL; GitHub sends
  // the browser back to `/auth/github`, whose page posts `callback`.
  githubStart: (body: { next?: string }) => send<{ authorize_url: string }>("POST", "/auth/github/start", body),
  githubCallback: (body: { code: string; state: string }) =>
    send<Redirect>("POST", "/auth/github/callback", body),
  login: (body: { email: string; password: string; next?: string }) =>
    send<Redirect>("POST", "/auth/login", body),
  logout: () => send<Redirect>("POST", "/auth/logout"),
  reset: (body: { token: string; password: string }) => send<Redirect>("POST", "/auth/reset", body),
};

export const accountApi = {
  get: () => get<AccountView>("/account"),
  update: (display_name: string) => send<AccountView>("PATCH", "/account", { display_name }),
  password: (body: { current: string; new: string }) =>
    send<unknown>("POST", "/account/password", body),
  orgs: () => get<OrgEntry[]>("/account/orgs"),
};

// ---- connected portals (portal/connect_routes.py, production only) ----------------

/** The consent page's query: what a local portal sent in the address. */
export interface ConnectRequest {
  redirect_uri: string;
  code_challenge: string;
  code_challenge_method: string;
  state: string;
  client_name: string;
  org?: string;
  project?: string;
}

/** `POST /api/connect/validate`. Both URLs the page navigates to come from the server. */
export interface ConnectValidated {
  ok: true;
  client_name: string;
  port: number;
  org: string | null;
  project: string | null;
  orgs: { slug: string; name: string; role: string }[];
  cancel_url: string;
}

export interface ConnectProject {
  org: string;
  org_name: string;
  slug: string;
  name: string;
  github_full_name: string | null;
  access_lost: boolean;
}

/** Why a connection token was revoked (`REVOKED_REASONS` in `portal/models.py`). */
export type RevokedReason =
  | "user_revoked"
  | "admin_revoked"
  | "removed_locally"
  | "member_removed"
  | "member_left"
  | "user_disabled"
  | "project_deleted"
  | "org_deleted"
  | "idle";

/** `GET /api/connect/tokens`: one of the caller's own connected portals. */
export interface MyConnection {
  uid: string;
  org: string | null;
  project: string | null;
  project_name: string | null;
  client_name: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  revoked_reason: RevokedReason | null;
}

/** `GET /api/projects/{slug}/connections`: a member's live token of that project. */
export interface ProjectConnection {
  uid: string;
  user_login: string | null;
  user_name: string | null;
  client_name: string;
  created_at: string;
  last_used_at: string | null;
}

export const connectApi = {
  validate: (body: ConnectRequest) => send<ConnectValidated>("POST", "/connect/validate", body),
  projects: () => get<ConnectProject[]>("/connect/projects"),
  authorize: (body: ConnectRequest & { org: string; project: string }) =>
    send<{ redirect: string; access_lost: boolean }>("POST", "/connect/authorize", body),
  tokens: () => get<MyConnection[]>("/connect/tokens"),
  revokeToken: (uid: string) => sendEmpty("DELETE", `/connect/tokens/${encodeURIComponent(uid)}`),
  projectConnections: (slug: string) =>
    get<ProjectConnection[]>(`/projects/${encodeURIComponent(slug)}/connections`),
  revokeProjectConnection: (slug: string, uid: string) =>
    sendEmpty("DELETE", `/projects/${encodeURIComponent(slug)}/connections/${encodeURIComponent(uid)}`),
};

export const orgsApi = {
  create: (body: { slug: string; name: string }) =>
    send<{ slug: string; url: string }>("POST", "/orgs", body),
  // The request's org (org host, owner, production only); the slug, typed, confirms it.
  remove: (confirm_slug: string) =>
    send<{ deleted: string; projects: number }>("DELETE", "/org", { confirm_slug }),
};

// ---- GitHub App (portal/github_app_routes.py, production only) -------------------

export interface GitHubInstallation {
  id: number;
  account_login: string;
  account_type: string;
  avatar_url: string | null;
  repository_selection: string;
}

export interface GitHubRepo {
  id: number;
  full_name: string;
  private: boolean;
  default_branch: string;
  /** Already a project in this org. */
  imported: boolean;
}

/** GitHub's redirect query, as `/auth/github-app` received it (minus `error`). */
export interface GitHubAppCallbackBody {
  code?: string;
  state?: string;
  iss?: string;
  installation_id?: number;
  setup_action?: "install" | "update" | "request";
}

export const githubApi = {
  // Org host: GitHub's authorize URL, or with `install` the app's install page.
  authorize: (install = false) => send<{ url: string }>("POST", "/github/app/authorize", { install }),
  // Base host: the server names where to go next (`null` for a request it did not start).
  callback: (body: GitHubAppCallbackBody) =>
    send<{ return_to: string | null; requested?: boolean }>("POST", "/github/app/callback", body),
  installations: () => get<{ installations: GitHubInstallation[] }>("/github/installations"),
  repos: (installationId: number, page = 1) =>
    get<{ repos: GitHubRepo[]; total_count: number; page: number }>(
      `/github/installations/${installationId}/repos?page=${page}`,
    ),
};

export const adminApi = {
  settings: () => get<AdminSettings>("/admin/settings"),
  orgs: () => get<AdminOrg[]>("/admin/orgs"),
  users: () => get<AdminUser[]>("/admin/users"),
  setAdmin: (uid: string, is_instance_admin: boolean) =>
    send<unknown>("PATCH", `/admin/users/${encodeURIComponent(uid)}`, { is_instance_admin }),
  setDisabled: (uid: string, disabled: boolean) =>
    send<unknown>("PATCH", `/admin/users/${encodeURIComponent(uid)}`, { disabled }),
  resetLink: (uid: string) =>
    send<{ url: string }>("POST", `/admin/users/${encodeURIComponent(uid)}/reset-link`),
};

// A call answered with `204 No Content` (no JSON body to parse).
async function sendEmpty(method: string, path: string): Promise<void> {
  const res = await fetch(`/api${path}`, init(method));
  if (!res.ok) throw await failure(res);
}

// Org members (`portal/member_routes.py`): org host, production only.
export const membersApi = {
  list: () => get<Member[]>("/org/members"),
  add: (body: { github_login: string; role: MemberRole }) => send<Member>("POST", "/org/members", body),
  setRole: (uid: string, role: MemberRole) =>
    send<Member>("PATCH", `/org/members/${encodeURIComponent(uid)}`, { role }),
  remove: (uid: string) => sendEmpty("DELETE", `/org/members/${encodeURIComponent(uid)}`),
  leave: () => sendEmpty("DELETE", "/org/membership"),
};

// ---- project-scoped calls ---------------------------------------------------

/**
 * The project-scoped API. Every data call lives under `/api/projects/<slug>`, so
 * a component holds a *slug*, not a base URL that could go stale on a switch.
 * Pair every use with a query key from {@link projectKey}.
 */
export function projectApi(slug: string) {
  const base = `/projects/${encodeURIComponent(slug)}`;
  return {
    slug,
    // ---- management (portal/routes.py) --------------------------------------
    config: () => get<ProjectConfigView>(`${base}/config`),
    putConfig: (body: ConfigPut) => send<ProjectConfigView>("PUT", `${base}/config`, body),
    init: (body: InitBody) => send<InitResult>("POST", `${base}/init`, body),
    requestScan: (body: { trigger?: "manual" | "hook" | "describe"; analyze?: boolean } = {}) =>
      send<{ run_id: number }>("POST", `${base}/scans`, body),
    scans: () => get<{ runs: ScanRunRow[] }>(`${base}/scans`),
    scanLog: (runId: number) => get<ScanLog>(`${base}/scans/${runId}/log`),
    cancelScan: (runId: number) =>
      send<{ run_id: number; was: "queued" | "running" }>("POST", `${base}/scans/${runId}/cancel`),
    rename: (name: string) => send<ProjectDetails>("PATCH", base, { name }),
    remove: (body: DeleteProjectBody = {}) => send<DeleteProjectResult>("DELETE", base, body),
    scanEstimate: () => get<ScanEstimate>(`${base}/scan-estimate`),
    streamScanEvents: (
      runId: number,
      onEvent: (event: ScanEvent, id: string | null) => void,
      opts: { signal?: AbortSignal; lastEventId?: string | null } = {},
    ) => streamScanEvents(`${base}/scans/${runId}/events`, onEvent, opts),

    search: (query: string, limit = 20) =>
      get<{ query: string; results: SearchResult[] }>(
        `${base}/search?q=${encodeURIComponent(query)}&limit=${limit}`,
      ),
    tree: (opts: { dir?: string; node?: string } = {}) => {
      const params = new URLSearchParams();
      if (opts.dir) params.set("dir", opts.dir);
      if (opts.node) params.set("node", opts.node);
      const qs = params.toString();
      return get<{ entries: TreeEntry[] }>(`${base}/tree${qs ? `?${qs}` : ""}`);
    },
    overview: (expanded = "") =>
      get<OverviewGraph>(`${base}/graph/overview?expanded=${encodeURIComponent(expanded)}`),
    ego: (qualified_name: string) =>
      get<EgoGraph>(`${base}/graph/ego?qualified_name=${q(qualified_name)}`),
    // qualified_name goes in the query string (a file node's qn is a path with
    // slashes; a path segment would break routing - see serve/routes.py).
    node: (qualified_name: string) =>
      get<NodeDetail>(`${base}/node?qualified_name=${q(qualified_name)}`),
    rationaleRead: (qualified_name: string) =>
      get<RationaleCard>(`${base}/node/rationale?qualified_name=${q(qualified_name)}`),
    rationaleGenerate: (qualified_name: string) =>
      send<RationaleCard>("POST", `${base}/node/rationale?qualified_name=${q(qualified_name)}`),
    evidence: (qualified_name: string, limit = 20) =>
      get<EvidenceResponse>(
        `${base}/node/evidence?qualified_name=${q(qualified_name)}&limit=${limit}`,
      ),
    history: (path: string, limit = 20) =>
      get<HistoryResponse>(`${base}/history?path=${encodeURIComponent(path)}&limit=${limit}`),

    // ---- chat ---------------------------------------------------------------
    chatProviders: () => get<ChatProvider[]>(`${base}/chat/providers`),
    chatModels: (provider: string) =>
      get<ChatModels>(`${base}/chat/models?provider=${encodeURIComponent(provider)}`),
    chatSessions: () => get<ChatSession[]>(`${base}/chat/sessions`),
    chatCreateSession: (body: { provider?: string; model?: string; title?: string }) =>
      send<ChatSession>("POST", `${base}/chat/sessions`, body),
    chatTranscript: (id: number) => get<ChatTranscript>(`${base}/chat/sessions/${id}`),
    chatUpdateSession: (
      id: number,
      body: { title?: string; provider?: string; model?: string },
    ) => send<ChatSession>("PATCH", `${base}/chat/sessions/${id}`, body),
    chatDeleteSession: async (id: number): Promise<void> => {
      const res = await fetch(`/api${base}/chat/sessions/${id}`, init("DELETE"));
      if (!res.ok) throw await failure(res);
    },
    streamChat: (
      sessionId: number,
      content: string,
      onEvent: (event: ChatEvent) => void,
      signal?: AbortSignal,
    ) => streamChat(`${base}/chat/sessions/${sessionId}/messages`, content, onEvent, signal),
  };
}

export type ProjectApi = ReturnType<typeof projectApi>;

/**
 * Query keys are prefixed with the project slug, so two projects can never share
 * a cache entry - isolation by construction, with no `queryClient.clear()` (which
 * races in-flight responses). Portal-level keys use the `@portal` prefix, which no
 * slug can equal (slugs match `[a-z0-9][a-z0-9-]*`).
 */
export const projectKey = (slug: string, ...parts: unknown[]) => [slug, ...parts] as const;
export const portalKey = (...parts: unknown[]) => ["@portal", ...parts] as const;

/**
 * POST a chat message and consume the SSE response, calling `onEvent` per frame.
 *
 * Hand-rolled rather than `EventSource`, which is GET-only and so cannot carry
 * the message body - and adding a dependency for ~20 lines of framing isn't
 * worth it. Must be called from a user action, never an effect: StrictMode
 * double-invokes effects in dev, which would send the turn twice.
 *
 * `signal` powers the Stop button. Aborting mid-stream leaves the server's
 * generator to persist whatever it has (GeneratorExit) - the transcript stays
 * consistent, so the caller can simply refetch it.
 *
 * Resolves when the stream ends. Rejects on a *transport* failure; a provider
 * failure arrives as an in-band `{type: "error"}` frame instead, because the
 * HTTP status was already committed before the first token.
 */
async function streamChat(
  path: string,
  content: string,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api${path}`, init("POST", { content }, signal));
  if (!res.ok) throw await failure(res);
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
        // failing the whole turn over - the UI already has everything before it.
      }
    }
  }
}

/**
 * Consume a scan run's SSE stream with `fetch` (never `EventSource`: the portal
 * requires the `X-WhyGraph-Client` header on every request, and `EventSource`
 * cannot send one). Mirrors {@link streamChat}'s framing, plus what a long-lived
 * GET needs: `id:` tracking and a `Last-Event-ID` resume header.
 *
 * `onEvent` receives each data frame, including the terminal `end` and the
 * `shutdown` frame (both carry their kind in the payload's `type`); `: heartbeat`
 * comments are ignored. Resolves when the stream closes, returning the last frame
 * id seen so the caller can reconnect with it. Rejects on a transport failure or a
 * non-2xx status.
 */
async function streamScanEvents(
  path: string,
  onEvent: (event: ScanEvent, id: string | null) => void,
  opts: { signal?: AbortSignal; lastEventId?: string | null } = {},
): Promise<string | null> {
  const headers: Record<string, string> = { ...CLIENT_HEADERS, Accept: "text/event-stream" };
  if (opts.lastEventId) headers["Last-Event-ID"] = opts.lastEventId;
  const res = await fetch(`/api${path}`, { method: "GET", headers, signal: opts.signal });
  if (!res.ok) throw await failure(res);
  if (!res.body) throw new ApiError(res.status, "streaming is unsupported here");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastId = opts.lastEventId ?? null;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let id: string | null = null;
      let data: string | null = null;
      for (const line of block.split("\n")) {
        if (line.startsWith("id:")) id = line.slice(3).trim();
        else if (line.startsWith("data:")) data = line.slice(5).trim();
      }
      if (id !== null) lastId = id;
      if (data === null) continue; // a `: heartbeat` comment frame
      try {
        onEvent(JSON.parse(data) as ScanEvent, id);
      } catch {
        // A truncated final frame is not worth failing the view over.
      }
    }
  }
  return lastId;
}
