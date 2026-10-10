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
  // A hard stop ended the turn (M2f-2); the terminal `done` frame follows it.
  | { type: "budget_exceeded"; scope: BudgetScope | null; message_id: number | null; message: string }
  | {
      type: "done";
      message_id: number | null;
      input_tokens: number | null;
      output_tokens: number | null;
      finish_reason: string | null;
    }
  // `code` (M2f-3): `no_llm_key` (with `provider`), `llm_unavailable` or `provider_error`;
  // `message` is the server's own text, kept for older clients and "Show details".
  | { type: "error"; message: string; code?: string; provider?: string };

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
  // Production, org host: set while the caller's membership in this org still shows
  // the first-visit banner (M2f-3); cleared by `DELETE /api/org/welcome`.
  welcome?: { org_name: string; role: MemberRole } | null;
  // The organization the request is in (M2b); null before setup. `reader` is an
  // instance admin looking at an org they are not a member of (read-only).
  org?: PortalOrg | null;
  // Production only (M2c); a missing `host_kind` means local mode.
  host_kind?: "local" | "base" | "org";
  base_url?: string;
  bootstrap_required?: boolean;
  // Local mode: this machine's name, the prefilled name a platform shows for the connection.
  hostname?: string;
  // Usage & cost (M2f-2): `null` before setup or without an org.
  usage?: StateUsage | null;
  error?: string;
}

/** One scope's spend this month against its budget (`state.usage.me` / `.org`). */
export interface UsageGauge {
  spent_usd: number;
  budget_usd: number | null;
  pct: number | null;
  hard_stop: boolean;
  /** The budget is spent and its hard stop is on: no new LLM spend for this scope. */
  blocked: boolean;
}

/** `GET /api/portal/state`'s `usage`: everything the sidebar and banners need. */
export interface StateUsage {
  /** `YYYY-MM`, UTC. */
  month: string;
  resets_at: string;
  /** The caller's own budget: production memberships only (`null` in local mode and for a reader). */
  me: UsageGauge | null;
  /** The org's spend: `org.usage` callers only (owners, org admins, readers; local mode's user). */
  org: UsageGauge | null;
  /** Projects at or over 50% of their budget, highest first (`org.usage` only). */
  projects_over: { slug: string; name: string; pct: number }[] | null;
}

export interface PortalOrg {
  slug: string;
  name: string;
  role: "owner" | "admin" | "member" | "reader" | (string & {});
  // What a member without a project grant gets on an unrestricted project (M2f-1).
  default_project_role?: "admin" | "contributor" | "viewer" | "none";
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
  // `null` for a production GitHub project: the server path is never sent there.
  root: string | null;
  remote_url: string | null;
  initialized: boolean;
  initialized_at: string | null;
  last_scan_at: string | null;
  created_at: string;
  root_status: "ok" | "missing" | "not_git";
  // `analyze` is whether the run may spend LLM tokens (a full scan).
  running_scan: { id: number; status: string; trigger: string; analyze?: boolean } | null;
  // Access (M2f-1): the caller's effective role and the project actions it grants.
  restricted?: boolean;
  my_role?: ProjectRole;
  permissions?: string[];
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
  // Why the caller cannot spend LLM tokens here (M2f-2): their role, or an exhausted
  // hard-stopped budget (`llm_block_scope` names it); the role wins when both apply.
  llm_block?: "role" | "budget_exceeded" | null;
  llm_block_scope?: BudgetScope | null;
  // This month's spend on the project: `project.usage` callers only.
  usage?: ProjectUsageBlock | null;
  // A GitHub import whose clone is still running (M2f-3).
  importing?: boolean;
  // Counts from the newest finished scan; `null` before the first one.
  last_scan_stats?: LastScanStats | null;
}

/** `ProjectSummary.last_scan_stats`: the coverage snapshot of the newest finished scan. */
export interface LastScanStats {
  commits: number;
  described_pct: number;
  rationale_cards: number;
  as_of: string;
}

/** What a budget caps, as refusals and alerts name it. */
export type BudgetScope = "org" | "project" | "member";

/** A project payload's `usage` block. */
export interface ProjectUsageBlock {
  month_spend_usd: number;
  budget: { monthly_usd: number; hard_stop: boolean } | null;
  pct: number | null;
}

/** How a linked project's connection to its platform stands (plan section 4.11). */
export type LinkStatus = "ok" | "access_lost" | "removed" | "revoked" | "unreachable" | "update_required";

/** A user's effective role on one project (M2f-1). */
export type ProjectRole = "admin" | "contributor" | "viewer";

/** `_summary`'s `link` of a `platform` project. The three URLs are built by the server. */
export interface ProjectLink {
  platform_origin: string;
  org: string;
  remote_slug: string;
  status: LinkStatus;
  status_reason: string | null;
  last_platform_head: string | null;
  // The caller's role on the platform project as last reported; `null` until seen.
  // A viewer has no chat there.
  project_role: ProjectRole | null;
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
  /** Whether the path exists (BUG-9: a missing folder answers `path_missing`). */
  exists?: boolean;
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
  // The scan queued for the new project; `null` when none was (`scan_error` says why).
  initial_run_id?: number | null;
  scan_error?: string;
}

export type AddProjectBody =
  | { source: "local"; path: string; name?: string; token?: string }
  | { source: "github"; installation_id: number; repo_id: number; name?: string }
  | { source: "platform"; link_id: string; path: string };

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
}

/** Write-only: a string sets, `null` deletes, an absent key leaves it alone. */
export interface SecretsPatch {
  llm?: Record<string, string | null>;
  github_token?: string | null;
}

export interface ConfigPut {
  config?: ConfigDict;
  secrets?: SecretsPatch;
}

/** Fields both the project config and the portal defaults payloads gain (M2f-3). */
export interface ConfigViewExtras {
  // The caller cannot change anything here.
  read_only?: boolean;
  // The caller may test keys (`project.configure`, not linked).
  can_test_keys?: boolean;
  // Configurers only: when each key was last used.
  key_last_used?: { llm: Record<string, string | null>; github_token: string | null };
}

export interface ProjectConfigView extends ConfigViewExtras {
  config: ConfigDict;
  secrets: SecretsView;
  import: ImportReport;
  // A linked project: the platform owns its settings.
  managed_on_platform?: boolean;
  effective_keys?: Record<string, KeyScope>;
  // Whether the organization layer has a key per provider (no hint).
  inherited?: Record<string, { set: boolean }>;
  // The GitHub remote and where the token for it comes from.
  github?: { remote: string | null; token: "project" | "org" | "none" };
  // Present on a PUT response only.
  hooks?: unknown;
  hooks_error?: string | null;
}

export interface DefaultsView extends ConfigViewExtras {
  config: ConfigDict;
  secrets: SecretsView;
  no_provider_key: boolean;
  // Present on a PUT response only: projects whose own key for a provider was
  // cleared because the global endpoint they inherit changed.
  cleared_project_keys?: { slug: string; provider: string }[];
}

/** The outcome words of a key test. */
export type KeyTestResult =
  | "ok"
  | "rejected"
  | "rate_limited"
  | "unreachable"
  | "no_repo_access"
  | "unexpected";

/** `POST .../keys/{provider}/test` and `.../github-token/test`. */
export interface KeyTestResponse {
  ok: boolean;
  result: KeyTestResult;
  scope_tested: string;
  checked_at: string;
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
  // A linked project's leftover `.whygraph/whygraph.db`, which is never opened.
  ignored_db?: string | null;
  // The scan queued after the initialization; `null` when none was (`scan_error` says why).
  initial_run_id?: number | null;
  scan_error?: string;
}

export interface ScanEstimate {
  commits: number;
  upper_bound: boolean;
  large_commits: number;
  model: { provider: string | null; model: string | null };
  // `tokens` and `cost` are `null` when `cost_hidden` is set (the caller lacks `project.usage`).
  tokens: {
    input: number;
    output: number;
    input_range: { low: number; high: number };
    output_range: { low: number; high: number };
  } | null;
  cost_hidden?: true;
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
  /** `"user"` when someone cancelled the run, `"budget"` when a hard stop did (vs. a merged or orphaned one). */
  cancelled_by?: string;
  /** What the run's LLM calls cost (M2f-2); a zero block when it made none. */
  usage?: ScanRunUsage;
  /** What the run was expected to cost (M2f-3); `project.usage` callers only. */
  estimate?: ScanRunEstimate;
  /** The coverage snapshot taken at the end of the run. */
  coverage?: { commits: number; described: number; described_pct: number; rationale_cards: number };
  /** A production GitHub project: whether the sync cloned the repository. */
  cloned?: boolean;
  /** Whether a sync went on to scan. */
  scanned?: boolean;
  [k: string]: unknown;
}

/** `summary.estimate` of a scan run: the cost guard's figure when it was queued. */
export interface ScanRunEstimate {
  commits: number;
  cost_usd: number | null;
  [k: string]: unknown;
}

/** `summary.usage` of a scan run. */
export interface ScanRunUsage {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  cost_source: CostSource | null;
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
  // A removed linked project (M2e): whether this machine's token was revoked on the platform.
  token_revoked?: boolean;
  // Why the token was or was not revoked (M2f-3); `token_revoked` stays for older readers.
  token_revoke_result?: string;
}

export interface ScanRunRow {
  id: number;
  kind: "scan" | "sync";
  trigger: string;
  analyze: boolean;
  status: ScanRunStatus;
  queued_at?: string | null;
  requested_by: RunPerson | null;
  cancelled_by?: RunPerson | null;
  started_at: string | null;
  finished_at: string | null;
  summary: ScanRunSummary | null;
  // `project.usage` callers only.
  cost_usd?: number | null;
  estimate_usd?: number | null;
}

/** A scan run's requester or canceller; `null` where it is typed means System. */
export interface RunPerson {
  uid: string;
  label: string;
}

/** One run, as the scan routes return it. */
export type ScanRun = ScanRunRow;

/** `GET .../scans` query filters (M2f-3). */
export interface ScanFilters {
  before?: number;
  limit?: number;
  status?: string[];
  trigger?: string[];
  type?: "full" | "quick" | "sync";
  requester?: string;
}

// The scan events stream (`portal/runner.py`): the child's JSONL events plus the
// runner's own `sync` / `error` / `end` / `shutdown` frames.
export type ScanEvent =
  | { type: "start"; phase_total: number; phases?: string[] }
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
  | {
      type: "sync";
      status: "cloning" | "fetching" | "ok" | "failed";
      moved?: boolean;
      error?: string;
      full_name?: string;
      cloned?: boolean;
    }
  | { type: "error"; message: string }
  | { type: "end"; run_id: number; status: ScanRunStatus; summary: ScanRunSummary | null }
  /** The stream was cut because the viewer lost access (signed out, removed, disabled); the run goes on. */
  | { type: "end"; run_id: number; status: null; summary: null; reason: "access_revoked" }
  | { type: "shutdown"; run_id: number };

// ---- transport --------------------------------------------------------------

/** One invalid field of a `422 invalid_request` (FastAPI's validation `detail`). */
export interface FieldError {
  /** Where, without the `body` / `query` / `path` prefix: `"title"`, `"grants.0.role"`. */
  path: string;
  msg: string;
}

/**
 * A failed API call. `status` is `0` when the portal could not be reached at all
 * (`code: "portal_unreachable"`). Never render `message` directly: the error
 * registry (`lib/apiErrors.ts`) words every code; the server's own text stays
 * in `serverMessage` for the "Show details" disclosure.
 */
export class ApiError extends Error {
  /** The server's own text (or the transport's), for "Show details". */
  readonly serverMessage: string;
  /** The invalid fields of a `422 invalid_request`; `undefined` otherwise. */
  readonly fields?: FieldError[];

  constructor(
    public status: number,
    message: string,
    // The portal's machine-readable `code` (`setup_required`, `not_found`, ...).
    public code?: string,
    // Extra top-level fields of the error body (`command`, `folder_suggestion`, `keys`, ...).
    public extra: Record<string, unknown> = {},
  ) {
    super(message);
    this.serverMessage = message;
    if (Array.isArray(extra.fields)) this.fields = extra.fields as FieldError[];
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

export type ErrorMode = "local" | "production";

// The portal's mode, for the error registry's mode-aware wording. Set once by the
// root gate when `GET /api/portal/state` lands (the QueryClient is module-local
// to main.tsx, so the registry cannot read the state from it).
let errorMode: ErrorMode = "local";

/** Tell the error registry which portal it words errors for. */
export function setErrorMode(mode: ErrorMode): void {
  errorMode = mode;
}

/** The mode {@link setErrorMode} last set (`"local"` until then). */
export function getErrorMode(): ErrorMode {
  return errorMode;
}

// An org host without a session answers `401 login_required`: send the browser to
// the base host's sign-in with a way back. `bad_credentials` (also 401) never gets
// here, and the base host's own /signin never redirects to itself. `reauth=1` tells
// the sign-in page the org host sent the person back (a session that ended, or a
// cookie the org host never received).
function redirectToSignIn(): void {
  if (!baseUrl) return;
  let base: URL;
  try {
    base = new URL(baseUrl);
  } catch {
    return;
  }
  if (window.location.origin === base.origin && window.location.pathname === "/signin") return;
  hardNavigate(`${base.origin}/signin?next=${encodeURIComponent(window.location.href)}&reauth=1`);
}

// FastAPI's validation `loc` starts with where the value came from; the rest is the field.
const LOC_SOURCES = new Set(["body", "query", "path", "header", "cookie"]);

function fieldErrors(detail: unknown[]): FieldError[] {
  return detail.map((item) => {
    const entry = (item ?? {}) as { loc?: unknown; msg?: unknown };
    const loc = Array.isArray(entry.loc) ? entry.loc.map(String) : [];
    const path = (LOC_SOURCES.has(loc[0]) ? loc.slice(1) : loc).join(".");
    return { path, msg: typeof entry.msg === "string" ? entry.msg : "is not valid" };
  });
}

async function failure(res: Response): Promise<ApiError> {
  const body: unknown = await res.json().catch(() => null);
  const fallback = res.statusText || `HTTP ${res.status}`;
  if (!body || typeof body !== "object" || Array.isArray(body)) return new ApiError(res.status, fallback);
  const { detail, error, code, ...extra } = body as Record<string, unknown>;
  const retryAfter = Number(res.headers?.get?.("Retry-After"));
  if (Number.isFinite(retryAfter) && retryAfter > 0 && extra.retry_after === undefined) {
    extra.retry_after = retryAfter;
  }
  const serverCode = typeof code === "string" ? code : undefined;
  if (res.status === 401 && serverCode === "login_required") redirectToSignIn();
  if (Array.isArray(detail)) {
    // A 422 from request validation: `detail` is a list of {loc, msg, type}.
    const fields = fieldErrors(detail);
    const text = fields.map((f) => (f.path ? `${f.path}: ${f.msg}` : f.msg)).join("; ");
    return new ApiError(res.status, text || (typeof error === "string" ? error : fallback), serverCode ?? "invalid_request", {
      ...extra,
      fields,
    });
  }
  const message = typeof detail === "string" ? detail : typeof error === "string" ? error : fallback;
  return new ApiError(res.status, message, serverCode, extra);
}

/**
 * `fetch`, with a network failure turned into `ApiError(0, ..., "portal_unreachable")`.
 * An `AbortError` (the chat's Stop, a cancelled query) is rethrown unchanged, so a
 * deliberate stop never reads as "portal unreachable".
 */
async function request(input: string, options: RequestInit): Promise<Response> {
  try {
    return await fetch(input, options);
  } catch (err) {
    if ((err as { name?: unknown } | null)?.name === "AbortError") throw err;
    if (err instanceof TypeError) {
      throw new ApiError(0, err.message || "the portal could not be reached", "portal_unreachable");
    }
    throw err;
  }
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
  return parse<T>(await request(`/api${path}`, init(method, body)));
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
  // Tests the organization layer's (local: the portal default) key for a provider.
  defaultsKeyTest: (provider: string) =>
    send<KeyTestResponse>("POST", `/portal/defaults/keys/${encodeURIComponent(provider)}/test`),
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
  // The caller's first visit to this org (the welcome flag).
  new?: boolean;
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
  // This month's spend (M2f-2): `GET /api/org/members` for `org.usage` callers only.
  month_spend_usd?: number;
  month_split?: { interactive: number; scans: number };
  // `org.members` holders only.
  grants?: MemberGrant[];
}

/** One project a member has a grant on (`project` is the slug). */
export interface MemberGrant {
  project: string;
  name: string;
  role: ProjectRole;
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
  // Production, base host (M2f-2).
  usage: () => get<AccountUsage>("/account/usage"),
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
  | "idle"
  | "project_access_removed";

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
  // Present with `include=revoked`.
  revoked_at?: string | null;
  revoked_reason?: RevokedReason | null;
}

/** The plan's name for a project's connection row. */
export type ConnectionRow = ProjectConnection;

export const connectApi = {
  validate: (body: ConnectRequest) => send<ConnectValidated>("POST", "/connect/validate", body),
  projects: () => get<ConnectProject[]>("/connect/projects"),
  authorize: (body: ConnectRequest & { org: string; project: string }) =>
    send<{ redirect: string; access_lost: boolean }>("POST", "/connect/authorize", body),
  tokens: () => get<MyConnection[]>("/connect/tokens"),
  revokeToken: (uid: string) => sendEmpty("DELETE", `/connect/tokens/${encodeURIComponent(uid)}`),
  projectConnections: (slug: string, opts: { includeRevoked?: boolean } = {}) =>
    get<ProjectConnection[]>(
      `/projects/${encodeURIComponent(slug)}/connections${opts.includeRevoked ? "?include=revoked" : ""}`,
    ),
  revokeProjectConnection: (slug: string, uid: string) =>
    sendEmpty("DELETE", `/projects/${encodeURIComponent(slug)}/connections/${encodeURIComponent(uid)}`),
};

// ---- connecting to a platform (portal/platform_routes.py, local mode only) ----------

export interface PlatformConnectBody {
  platform_url: string;
  client_name?: string;
  org?: string;
  project?: string;
}

/** What the platform sent back to `/connect/callback`. */
export interface PlatformCallbackBody {
  state: string;
  iss?: string;
  code?: string;
  error?: string;
}

export interface PendingPlatformLink {
  link_id: string;
  platform_origin: string;
  org: string;
  project: { slug: string; name: string; [key: string]: unknown };
  clone_url: string;
  clone_command: string;
  /** A genuine name collision: that slug is a project here this link cannot reconnect. */
  slug_taken: boolean;
  /** The already-linked project this link reconnects (a new token), when it is one. */
  reconnect: { path: string; slug: string } | null;
  candidates: { path: string; name: string; match: "origin" }[];
  other_repos: { path: string; name: string }[];
}

export const platformApi = {
  connect: (body: PlatformConnectBody) =>
    send<{ authorize_url: string; platform_origin: string; known_platform: boolean }>(
      "POST",
      "/platform/connect",
      body,
    ),
  callback: (body: PlatformCallbackBody) => send<{ link_id: string }>("POST", "/platform/callback", body),
  pending: (linkId: string) => get<PendingPlatformLink>(`/platform/pending/${encodeURIComponent(linkId)}`),
  abandon: (linkId: string) =>
    send<{ revoked: boolean }>("DELETE", `/platform/pending/${encodeURIComponent(linkId)}`),
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

/** Query of `GET /api/github/installations/{id}/repos`. */
export interface InstallationReposQuery {
  q?: string;
  refresh?: boolean;
  page?: number;
}

/** Its answer; `total_count` is the filtered count. */
export interface InstallationRepos {
  repos: GitHubRepo[];
  total_count: number;
  page: number;
  per_page?: number;
  truncated?: boolean;
  // How many more repositories this org may import this hour.
  imports_left?: number;
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
  // A bare number is the page (the form older callers use).
  repos: (installationId: number, opts: number | InstallationReposQuery = {}) => {
    const o = typeof opts === "number" ? { page: opts } : opts;
    const params = new URLSearchParams({ page: String(o.page ?? 1) });
    if (o.q) params.set("q", o.q);
    if (o.refresh) params.set("refresh", "1");
    return get<InstallationRepos>(`/github/installations/${installationId}/repos?${params}`);
  },
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
  const res = await request(`/api${path}`, init(method));
  if (!res.ok) throw await failure(res);
}

// Org members (`portal/member_routes.py`): org host, production only.
export const membersApi = {
  list: () => get<Member[]>("/org/members"),
  // An account is added at once (a `Member`); anyone else gets an invitation (`pending: true`).
  add: (body: { github_login: string; role: MemberRole; grants?: ProjectGrant[] }) =>
    send<Member | PendingInvite>("POST", "/org/members", body),
  setRole: (uid: string, role: MemberRole) =>
    send<Member>("PATCH", `/org/members/${encodeURIComponent(uid)}`, { role }),
  remove: (uid: string) => sendEmpty("DELETE", `/org/members/${encodeURIComponent(uid)}`),
  leave: () => sendEmpty("DELETE", "/org/membership"),
};

/** A project grant carried by an invitation or a new member. */
export interface ProjectGrant {
  project: string;
  role: ProjectRole;
}

/** One invitation as `GET /api/org/invitations` lists it (never an email). */
export interface Invitation {
  uid: string;
  github_login: string;
  role: MemberRole;
  status: "open" | "expired" | "redeemed" | "revoked";
  invited_by: { uid: string; display_name: string; github_login: string | null } | null;
  created_at: string;
  expires_at: string;
  redeemed_at: string | null;
  revoked_at: string | null;
  grants: ProjectGrant[];
}

/** The `201` of an invite for someone with no account yet. */
export type PendingInvite = Invitation & { pending: true };

export const invitationsApi = {
  list: () => get<Invitation[]>("/org/invitations"),
  revoke: (uid: string) => sendEmpty("DELETE", `/org/invitations/${encodeURIComponent(uid)}`),
};

// Org settings (`PATCH /api/org`, `POST /api/org/transfer`; production, org host).
export const orgSettingsApi = {
  patch: (body: { name?: string; default_project_role?: "contributor" | "viewer" | "none" }) =>
    send<{ slug: string; name: string; default_project_role: string }>("PATCH", "/org", body),
  transfer: (user_uid: string, confirm_slug: string) =>
    send<unknown>("POST", "/org/transfer", { user_uid, confirm_slug }),
};

/** One person on a project's access list (`GET /api/projects/<slug>/access`). */
export interface AccessPerson {
  uid: string;
  login: string | null;
  name: string;
  avatar: string | null;
  org_role: MemberRole;
  project_role: ProjectRole | null;
  source: "org_admin" | "grant" | "default";
  // Their spend on this project this month (M2f-2).
  month_spend_usd?: number;
}

export interface ProjectAccessData {
  restricted: boolean;
  org_default: "admin" | "contributor" | "viewer" | "none";
  people: AccessPerson[];
  invitations: { uid: string; github_login: string; role: ProjectRole; created_at: string; expires_at: string }[];
}

export const accessApi = (slug: string) => {
  const base = `/projects/${encodeURIComponent(slug)}/access`;
  return {
    get: () => get<ProjectAccessData>(base),
    setRestricted: (restricted: boolean) => send<{ restricted: boolean }>("PATCH", base, { restricted }),
    setGrant: (uid: string, role: ProjectRole) => send<unknown>("PUT", `${base}/${encodeURIComponent(uid)}`, { role }),
    removeGrant: (uid: string) => sendEmpty("DELETE", `${base}/${encodeURIComponent(uid)}`),
  };
};

/** One persisted security event (`portal/audit_store.py` `_row`). */
export interface AuditEventRow {
  id: number;
  created_at: string;
  org: string | null;
  actor: { uid: string | null; label: string | null } | null;
  event: string;
  target: string | null;
  // A user uid as "Name (@login)", a project slug as its name.
  target_label?: string | null;
  ip: string | null;
  fields: Record<string, unknown>;
}

export interface AuditFilters {
  event?: string;
  actor?: string;
  from?: string;
  to?: string;
  before?: number;
}

function auditQuery(f: AuditFilters): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v !== undefined && v !== "") params.set(k, String(v));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export const auditApi = {
  org: (f: AuditFilters = {}) =>
    get<{ events: AuditEventRow[]; next: number | null }>(`/org/audit${auditQuery(f)}`),
  admin: (f: AuditFilters = {}) =>
    get<{ events: AuditEventRow[]; next: number | null }>(`/admin/audit${auditQuery(f)}`),
  // A fetch (the `X-WhyGraph-Client` header is required, so a plain link would be refused).
  csv: async (f: AuditFilters = {}): Promise<Blob> => {
    const { before: _before, ...rest } = f;
    const res = await request(`/api/org/audit.csv${auditQuery(rest)}`, init("GET"));
    if (!res.ok) throw await failure(res);
    return res.blob();
  },
};

// ---- usage & cost (portal/usage_routes.py, portal/budget_routes.py; M2f-2) ------
//
// Money is a JSON number (costs to 6 places, budgets to cents); a token sum is
// `null` when no row in it reported that kind of token.

export type UsageGroup = "project" | "member" | "task" | "model" | "source" | "machine" | "day";
export type UsageTask = "analyze" | "rationale" | "chat";
export type UsageSource = "scan" | "explorer" | "chat" | "mcp" | "agent";
export type CostSource = "provider" | "estimated" | "unpriced";
export type KeyScope = "project" | "org" | "environment" | "none";

/** The query of every usage route. `from` is inclusive, `to` exclusive (UTC days); both default to this month. */
export interface UsageQuery {
  from?: string;
  to?: string;
  project?: string;
  /** A member's uid, or `"system"`. */
  member?: string;
  task?: UsageTask | string;
  source?: UsageSource | string;
  /** The served model (else the requested one). */
  model?: string;
  scan_run?: number;
  /** A chat session id: needs `project` (ids are per project). */
  chat_session?: number;
  group?: UsageGroup;
  sort?: "time" | "cost";
  /** The previous page's `next`. */
  before?: string;
}

export interface UsageTotals {
  calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  reasoning_tokens: number | null;
  cost_usd: number;
  /** Calls with no price: their tokens count, their cost does not. */
  unpriced_calls: number;
}

/** Interactive (chat, generate, agent calls, backfill) vs. scans started. */
export interface UsageSplit {
  interactive: { calls: number; cost_usd: number };
  scans: { calls: number; cost_usd: number };
}

export interface UsageDay {
  day: string;
  calls: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
}

/**
 * One row of a `group=` breakdown, sorted costliest first (`day` by date).
 * `key` is the filter value: a project slug, a member uid, a connection id, or the
 * task / source / model itself; `null` for System, a deleted member, and the
 * machine buckets "Portal" / "Unknown machine".
 */
export interface UsageGroupRow extends UsageTotals, UsageSplit {
  key: string | number | null;
  label: string;
  /** `group=member` only. */
  top_project?: { slug: string; name: string; cost_usd: number } | null;
}

export interface UsageReport {
  range: { from: string; to: string };
  totals: UsageTotals;
  split: UsageSplit;
  /** One entry per day of the range, empty days included. */
  series: UsageDay[];
  /** Empty without `group`. */
  groups: UsageGroupRow[];
}

/** One LLM call (`GET /api/usage/calls`). */
export interface UsageCall {
  id: number;
  created_at: string;
  project_slug: string | null;
  project_name: string | null;
  actor_label: string | null;
  user_uid: string | null;
  source: UsageSource | string;
  task: UsageTask | string;
  provider: string;
  model_requested: string | null;
  model_served: string | null;
  key_scope: KeyScope | string;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  reasoning_tokens: number | null;
  cost_usd: number | null;
  cost_source: CostSource | string;
  price_version: string | null;
  scan_run_id: number | null;
  chat_session_id: number | null;
  /** The linked portal's machine name, for an agent call. */
  client_name: string | null;
  /** A commit SHA, a file path or a qualified name; never prompt text. */
  subject: string | null;
}

export interface UsageCallsPage {
  items: UsageCall[];
  /** Pass as `before` for the next page; `null` on the last. */
  next: string | null;
}

/** `GET /api/projects/{slug}/usage`: one project, with its members' spend (production). */
export interface ProjectUsageReport extends Omit<UsageReport, "groups"> {
  members: UsageGroupRow[];
}

/** A downloaded CSV, and whether the server cut it at 50,000 rows. */
export interface UsageCsv {
  blob: Blob;
  filename: string;
  truncated: boolean;
}

export interface Budget {
  monthly_usd: number;
  hard_stop: boolean;
  /** `null` for the member default (it caps each member separately). */
  spent_usd: number | null;
  pct: number | null;
}

export interface BudgetAlert {
  scope: BudgetScope;
  label: string | null;
  threshold: 50 | 75 | 100 | number;
  crossed_at: string;
  spent_usd: number | null;
}

/** `GET /api/budgets`. Local mode has no member parts. */
export interface BudgetsView {
  month: string;
  resets_at: string;
  org: Budget | null;
  member_default: Budget | null;
  members: (Budget & { uid: string; label: string | null })[];
  projects: (Budget & { slug: string; name: string })[];
  /** This month's threshold crossings. */
  alerts: BudgetAlert[];
  /** This month's calls with no price, which no money budget counts. */
  unpriced_calls: number;
}

export interface BudgetBody {
  monthly_usd: number;
  hard_stop: boolean;
}

/** A budget write's target. */
export type BudgetTarget =
  | { scope: "org" }
  | { scope: "member_default" }
  | { scope: "member"; uid: string }
  | { scope: "project"; slug: string };

/** One row of the merged price table (USD per million tokens). */
export interface PriceRow {
  provider: string;
  model: string;
  input_per_mtok: number | null;
  output_per_mtok: number | null;
  cache_read_per_mtok: number | null;
  cache_write_per_mtok: number | null;
  origin: "bundled" | "override";
  /** An override's last change; `null` for a bundled row (`as_of` dates those). */
  updated_at: string | null;
}

export interface PricesView {
  as_of: string;
  rows: PriceRow[];
}

export interface PriceBody {
  provider: string;
  model: string;
  input_per_mtok: number;
  output_per_mtok: number;
  cache_read_per_mtok?: number | null;
  cache_write_per_mtok?: number | null;
}

/** `GET /api/account/usage` (base host, production): the caller's spend this month in each org. */
export interface AccountUsage {
  month: string;
  resets_at: string;
  orgs: {
    slug: string;
    name: string;
    /** The org's origin; its My usage page is `url + "/usage/me"`. */
    url: string;
    spent_usd: number;
    calls: number;
    budget_usd: number | null;
    pct: number | null;
    hard_stop: boolean;
  }[];
}

function usageQuery(f: UsageQuery): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/** The file name a `Content-Disposition` header names, or `fallback`. */
function attachmentName(res: Response, fallback: string): string {
  const header = res.headers.get("content-disposition") ?? "";
  const match = /filename="?([^";]+)"?/i.exec(header);
  return match?.[1] ?? fallback;
}

async function fetchCsv(path: string, fallback: string): Promise<UsageCsv> {
  const res = await request(`/api${path}`, init("GET"));
  if (!res.ok) throw await failure(res);
  return {
    blob: await res.blob(),
    filename: attachmentName(res, fallback),
    truncated: res.headers.get("x-whygraph-truncated") === "1",
  };
}

/**
 * The usage read routes for one viewpoint: `org` (`/api/usage*`, `org.usage`) or
 * `me` (`/api/usage/me*`, production; the server forces the caller and ignores
 * `member`). Both answer the same shapes.
 */
export function usageApi(scope: "org" | "me") {
  const base = scope === "me" ? "/usage/me" : "/usage";
  return {
    report: (f: UsageQuery = {}) => get<UsageReport>(`${base}${usageQuery(f)}`),
    calls: (f: UsageQuery = {}) => get<UsageCallsPage>(`${base}/calls${usageQuery(f)}`),
    // A fetch, not a link: the `X-WhyGraph-Client` header is required. `before` is not a CSV filter.
    csv: (f: UsageQuery = {}) => {
      const { before: _before, sort: _sort, ...rest } = f;
      return fetchCsv(`${base}.csv${usageQuery(rest)}`, scope === "me" ? "whygraph-my-usage.csv" : "whygraph-usage.csv");
    },
  };
}

/** Where a budget target is written. */
function budgetPath(target: BudgetTarget): string {
  switch (target.scope) {
    case "org":
      return "/budgets/org";
    case "member_default":
      return "/budgets/member-default";
    case "member":
      return `/budgets/members/${encodeURIComponent(target.uid)}`;
    case "project":
      return `/projects/${encodeURIComponent(target.slug)}/budget`;
  }
}

export const budgetsApi = {
  get: () => get<BudgetsView>("/budgets"),
  /** `422 invalid_amount` / `budget_above_org`, `409 budget_below_children`, `403 forbidden`, `404 not_member`. */
  put: (target: BudgetTarget, body: BudgetBody) => send<Budget>("PUT", budgetPath(target), body),
  remove: (target: BudgetTarget) => sendEmpty("DELETE", budgetPath(target)),
};

export const pricesApi = {
  get: () => get<PricesView>("/prices"),
  /** `422 bad_provider` / `bad_model` / `invalid_price` (with `field`). */
  put: (body: PriceBody) => send<PriceRow>("PUT", "/prices", body),
  // By query, not path: OpenRouter model ids contain `/`.
  revert: (provider: string, model: string) =>
    sendEmpty("DELETE", `/prices?${new URLSearchParams({ provider, model })}`),
};

export const projectUsageApi = (slug: string) => ({
  report: (f: Omit<UsageQuery, "project" | "group" | "sort" | "before"> = {}) =>
    get<ProjectUsageReport>(`/projects/${encodeURIComponent(slug)}/usage${usageQuery(f)}`),
});

// ---- project overview (portal/overview_routes.py; M2f-3) ----------------------

export interface OverviewCoveragePoint {
  run_id: number;
  at: string;
  commits: number;
  described: number;
  described_pct: number;
  rationale_cards: number;
}

export type OverviewEventKind = "import" | "first_scan" | "full_scan" | "describe" | "failed" | "budget_stop";

/** `GET /api/projects/{slug}/overview`. */
export interface ProjectOverview {
  coverage: { points: OverviewCoveragePoint[] };
  events: { run_id: number; at: string; kind: OverviewEventKind }[];
  last_failure: { run_id: number; at: string; message: string } | null;
  // `project.usage` callers only.
  usage: {
    month: string;
    spent_usd: number;
    budget_usd: number | null;
    pct: number | null;
    by_task: { task: string; calls: number; cost_usd: number }[];
  } | null;
  agents: {
    days: { day: string; mcp: number; agent: number }[];
    total_calls: number;
    llm_calls: number;
    // `null` without `project.usage`.
    llm_cost_usd: number | null;
    by_kind: { kind: string; calls: number }[];
    // Production, `project.usage` only.
    people: { label: string; calls: number; last_day: string | null }[] | null;
    // Production, `project.configure` only.
    connections: { client_name: string; user_label: string | null; last_used_at: string | null }[] | null;
    last_call_day: string | null;
  };
}

// ---- onboarding (portal/onboarding_routes.py; M2f-3) ---------------------------

export type OnboardingItemId = "llm_key" | "project" | "agent" | "github" | "invite";

/** `GET /api/onboarding`: an item that does not apply is omitted. */
export interface Onboarding {
  items: { id: OnboardingItemId; done: boolean; can_act: boolean }[];
}

/** `GET /api/orgs/slug-check?slug=` (production, base host). */
export interface SlugCheck {
  slug: string;
  available: boolean;
  reason: "invalid" | "reserved" | "taken" | null;
}

export const onboardingApi = {
  onboarding: () => get<Onboarding>("/onboarding"),
  // Clears the caller's own welcome flag in the request's org (production, org host).
  dismissWelcome: () => sendEmpty("DELETE", "/org/welcome"),
  slugCheck: (slug: string) => get<SlugCheck>(`/orgs/slug-check?slug=${encodeURIComponent(slug)}`),
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
    scans: (f: ScanFilters = {}) => {
      const params = new URLSearchParams();
      if (f.before !== undefined) params.set("before", String(f.before));
      if (f.limit !== undefined) params.set("limit", String(f.limit));
      if (f.status?.length) params.set("status", f.status.join(","));
      if (f.trigger?.length) params.set("trigger", f.trigger.join(","));
      if (f.type) params.set("type", f.type);
      if (f.requester) params.set("requester", f.requester);
      const qs = params.toString();
      return get<{ runs: ScanRunRow[]; next?: number | null }>(`${base}/scans${qs ? `?${qs}` : ""}`);
    },
    scan: (runId: number) => get<ScanRun>(`${base}/scans/${runId}`),
    // Named apart from the graph `overview(expanded)` below.
    projectOverview: () => get<ProjectOverview>(`${base}/overview`),
    // Tests the key the project would use for a provider.
    keyTest: (provider: string) =>
      send<KeyTestResponse>("POST", `${base}/keys/${encodeURIComponent(provider)}/test`),
    // Local mode only: tests the effective GitHub token against the project's remote.
    githubTokenTest: () => send<KeyTestResponse>("POST", `${base}/github-token/test`),
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
      const res = await request(`/api${base}/chat/sessions/${id}`, init("DELETE"));
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
  const res = await request(`/api${path}`, init("POST", { content }, signal));
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
  const res = await request(`/api${path}`, { method: "GET", headers, signal: opts.signal });
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
