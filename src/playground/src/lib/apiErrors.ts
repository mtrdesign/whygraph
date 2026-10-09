import { ApiError, getErrorMode, type ErrorMode } from "../api";
import { budgetNoticeText } from "./budgetBanner";
import { revokedLabel } from "./connections";
import { formatUsd } from "./format";

// The one error-code registry (M2f-3 plan section 4.3, Appendix C). Every code
// the backend can return (`api-error-codes.json`, kept equal to the backend by
// `tests/test_error_codes.py`) has an entry in `MESSAGES`; a component renders a
// failure through `errorInfo` / `errorMessage` / `ErrorState`, never `err.message`.
//
// Ownership: after M2f-3 S3 only the backend-lane steps edit `MESSAGES` and the
// JSON (each code they add or remove, in both); SPA steps call `errorInfo`.

/** Where the error is shown: a few codes mean different things on different pages. */
export type ErrorContext = "generic" | "auth" | "add-project" | "github" | "link" | "chat" | "usage";

export type ErrorTone = "error" | "warn" | "info";

export interface ErrorAction {
  label: string;
  /** An in-app route. */
  to?: string;
  /** A full address. */
  href?: string;
  /** Trying again may work: `ErrorState` offers its Retry. */
  retry?: boolean;
}

export interface ErrorInfo {
  /** The server's code, or a synthetic one (`portal_unreachable`, `invalid_request`, `http_404`...). */
  code: string | null;
  /** Short: "No access", "Not found". */
  title: string;
  /** The human sentence; never the raw server text for a known code. */
  message: string;
  tone: ErrorTone;
  action?: ErrorAction;
  /** The server's own text, for "Show details"; `null` when there is nothing more to say. */
  detail: string | null;
}

interface Entry {
  message: string;
  title?: string;
  tone?: ErrorTone;
  action?: ErrorAction;
}

type Resolver = string | Entry | ((err: ApiError, ctx: ErrorContext, mode: ErrorMode) => string | Entry);

const RETRY: ErrorAction = { label: "Try again", retry: true };

/** Codes the SPA makes up itself; they are in `MESSAGES` but not in the backend's JSON. */
export const SYNTHETIC_CODES: readonly string[] = ["portal_unreachable", "invalid_request"];

// ---- helpers for the entries ---------------------------------------------------

/** One entry of `409 budget_below_children`'s `children`. */
interface BudgetChild {
  scope: string;
  monthly_usd: number;
  slug?: string;
  name?: string;
  uid?: string;
  label?: string;
}

function childName(child: BudgetChild): string {
  const who =
    child.name ?? child.label ?? (child.scope === "member_default" ? "the member default" : (child.slug ?? child.uid));
  if (!who) return "";
  return typeof child.monthly_usd === "number" ? `${who} (${formatUsd(child.monthly_usd)})` : who;
}

const PRICE_FIELDS: Record<string, string> = {
  input_per_mtok: "The input price",
  output_per_mtok: "The output price",
  cache_read_per_mtok: "The cache read price",
  cache_write_per_mtok: "The cache write price",
};

const PROVIDER_NAMES: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  openrouter: "OpenRouter",
  deepseek: "DeepSeek",
  ollama: "Ollama",
};

const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

/** The provider a `no_llm_key` names: the body's `provider`, else the "(name)" of the server's text. */
function providerOf(err: ApiError): string | null {
  const raw = str(err.extra.provider) ?? /\(([a-z]+)\)/.exec(err.serverMessage)?.[1] ?? null;
  if (!raw) return null;
  return PROVIDER_NAMES[raw] ?? raw;
}

/** "Try again in N minutes." from the `Retry-After` the transport kept, else a few minutes. */
function retryLine(err: ApiError): string {
  const seconds = typeof err.extra.retry_after === "number" ? err.extra.retry_after : null;
  if (seconds === null) return "Try again in a few minutes.";
  if (seconds < 60) return "Try again in a minute.";
  const minutes = Math.ceil(seconds / 60);
  return `Try again in ${minutes} minute${minutes === 1 ? "" : "s"}.`;
}

const portalUnreachable = (mode: ErrorMode): Entry => ({
  title: "Can't reach WhyGraph",
  message:
    mode === "production"
      ? "Can't reach WhyGraph. Check your connection."
      : "Can't reach the WhyGraph portal. Check that it is running: whygraph status.",
  action: RETRY,
});

const forbidden = (err: ApiError, mode: ErrorMode): Entry => {
  if (mode !== "production") return { title: "Not available", message: "This action isn't available here." };
  if (/no longer a member/.test(err.serverMessage)) {
    return { title: "No access", message: "You are no longer a member of that organization." };
  }
  const projectAction = /on this project/.test(err.serverMessage);
  return {
    title: "No access",
    message: projectAction
      ? "You don't have permission to do that. A project admin can."
      : "You don't have permission to do that. An organization owner or admin can.",
  };
};

const GENERIC_5XX = "WhyGraph hit an error. Try again; if it keeps happening, check the portal log.";

// ---- the table -------------------------------------------------------------------

export const MESSAGES: Record<string, Resolver> = {
  // Synthetic (made up by the transport, never sent by the backend)
  portal_unreachable: (_err, _ctx, mode) => portalUnreachable(mode),
  invalid_request: (err) => {
    const fields = err.fields ?? [];
    const list = fields.map((f) => (f.path ? `${f.path}: ${f.msg}` : f.msg)).join("; ");
    return {
      title: "Check the details",
      message: list ? `Some fields aren't valid: ${list}.` : "Some fields aren't valid.",
    };
  },

  // Sign-in, accounts and passwords
  weak_password: "That password is too short. Use at least 15 characters - a passphrase works well.",
  common_password: "That password is too common. Pick a longer, less guessable passphrase.",
  bad_email: "Enter a valid email address.",
  email_taken: "An account with this email already exists.",
  bad_credentials: (err) =>
    err.status === 403 ? "That is not your current password." : "Incorrect email or password.",
  bad_secret: "That is not the bootstrap secret.",
  bad_token: (_err, ctx) =>
    ctx === "auth"
      ? "This reset link is invalid, already used or expired. Ask an administrator for a new one."
      : "GitHub rejected this token. Check that it has not expired or been revoked.",
  oauth_state: (_err, ctx) =>
    ctx === "github"
      ? "This authorization expired or was started in another browser. Start again from your organization's import page."
      : "The sign-in expired - try again.",
  github_auth_failed: (_err, ctx) =>
    ctx === "github"
      ? "GitHub did not confirm the authorization. Start again from your organization's import page."
      : "GitHub did not accept the sign-in. Try again.",
  github_unavailable: { message: "GitHub could not be reached. Try again in a moment.", action: RETRY },
  github_2fa_required:
    "Your GitHub account needs two-factor authentication turned on before you can sign in to WhyGraph.",
  account_disabled: "This account is disabled. Ask an instance administrator to enable it.",
  no_password: "This account signs in with GitHub, so it has no password.",
  login_required: { title: "Signed out", message: "Your session has ended. Sign in again." },
  setup_required: "WhyGraph isn't set up yet. Finish the setup page first.",
  bootstrap_required: "This WhyGraph instance isn't set up yet. Finish the setup page first.",
  throttled: (err) => ({ title: "Too many attempts", tone: "warn", message: `Too many attempts. ${retryLine(err)}` }),
  forbidden: (err, _ctx, mode) => forbidden(err, mode),

  // Organizations
  bad_slug: (err) =>
    /reserved/.test(err.serverMessage)
      ? "That name is reserved. Pick another."
      : "Use 1-40 lowercase letters, digits and dashes, not starting or ending with a dash.",
  slug_taken: (_err, ctx) =>
    ctx === "link"
      ? "A project with this name already exists on this machine. Choose another name."
      : "That URL name is already taken.",
  bad_name: (err) =>
    /at most/.test(err.serverMessage)
      ? "Use an organization name of at most 200 characters."
      : /control characters/.test(err.serverMessage)
        ? "Remove the control characters from the name."
        : "Enter an organization name.",
  confirm_slug: "Type the organization's slug exactly to confirm.",
  self_transfer: "You already own this organization. Pick another member.",
  already_owner: "They are already an owner.",

  // Members, invitations and project access
  bad_login: "That is not a GitHub username.",
  no_such_github_user: (err) => {
    const login = str(err.extra.login);
    return login ? `GitHub has no user called @${login.replace(/^@/, "")}.` : "GitHub has no user with that username.";
  },
  grants_for_org_admin: "Owners and admins already see every project; remove the project access entries.",
  github_rate_limited: "GitHub's rate limit was reached. Try again in a few minutes.",
  already_invited: "They already have an open invitation. Revoke it first to send a new one.",
  org_admin: "Org admins and owners already have every project role.",
  already_member: "They are already a member of this organization.",
  user_disabled: "This account is disabled. An instance administrator must enable it first.",
  last_owner: "This is the organization's last owner. Make someone else an owner first.",
  not_member: "That person is no longer a member of this organization.",
  owner_required: "Only an owner can make someone an owner, or change or remove an owner.",
  self_disable: "You cannot disable your own account.",
  last_admin: "This is the last enabled instance administrator. Make someone else an administrator first.",
  bad_role: "That role doesn't exist here.",
  duplicate_grant: "A project is listed twice. Keep one access entry per project.",
  no_grant: "That person has no specific access to this project.",
  no_such_invitation: "That invitation no longer exists.",
  invitation_closed: "This invitation was already used or revoked.",
  no_such_project: "A selected project no longer exists in this organization.",

  // Adding and importing projects
  not_shared: "This folder is not shared with the portal.",
  not_git: "This folder is not a git repository.",
  protected: "This path overlaps the portal's own data folder.",
  not_linked: (_err, ctx) =>
    ctx === "add-project"
      ? "This repository has no GitHub origin, so a GitHub token does not apply."
      : "This project's link is gone. Add it again.",
  duplicate: (_err, ctx) =>
    ctx === "github"
      ? "This repository is already a project in this organization."
      : "This repository is already a project.",
  clone_failed: {
    message: "WhyGraph couldn't clone this repository. Check that it still exists and can be read, then try again.",
    action: RETRY,
  },
  no_access: (_err, ctx) =>
    ctx === "add-project"
      ? "This token cannot read the repository. Grant it read access to the repository's contents (and pull requests and issues, for the GitHub crawl)."
      : "That repository is not available to you through this installation. Check that you can read it on GitHub and that the WhyGraph app covers it.",
  not_found: (_err, ctx) =>
    ctx === "add-project" || ctx === "github"
      ? "WhyGraph can't find that repository, or it is not visible to this token. GitHub answers both the same way."
      : ctx === "link"
        ? "That connection no longer exists."
        : { title: "Not found", message: "That no longer exists." },
  github_error: { message: "GitHub answered with an error. Try again in a moment.", action: RETRY },
  source_not_allowed: "This kind of project is not supported here. Remove the project.",
  github_access_lost: "WhyGraph can no longer read this repository on GitHub. Reconnect it on GitHub, then scan again.",
  github_authorization_required: "Connect GitHub first: WhyGraph needs your authorization to list your repositories.",
  github_account_mismatch:
    "You authorized a different GitHub account than the one you signed in with. Switch accounts on GitHub and try again.",
  github_required: "Importing from GitHub needs an account that signs in with GitHub.",
  github_app_not_configured: "This portal has no GitHub App configured. Ask an instance administrator.",
  start_from_portal: "Open your organization in WhyGraph and choose Import from GitHub.",
  tracked_whygraph_state: (err) => {
    const paths = Array.isArray(err.extra.paths) ? (err.extra.paths as unknown[]).filter((p) => typeof p === "string") : [];
    const which = paths.length > 0 ? ` (${paths.join(", ")})` : " (.whygraph/ or .codegraph/)";
    return `This repository tracks WhyGraph's own state${which}. Remove it from the repository to import it.`;
  },
  busy: (_err, ctx) =>
    ctx === "link"
      ? { message: "The platform is busy right now. Try again in a moment.", action: RETRY }
      : ctx === "github" || ctx === "add-project"
        ? { message: "Another import took this name at the same moment. Try again.", action: RETRY }
        : { message: "A sync is finishing - try again in a minute.", action: RETRY },

  // Projects: setup, settings, removal, scans
  not_initialized: { title: "Setup not finished", tone: "warn", message: "This project isn't set up yet. Finish setting it up first." },
  root_missing: "The project's folder is missing. Share its folder again, or remove the project.",
  unsafe_path: { title: "A symbolic link is in the way", message: "WhyGraph refused a symbolic link that leads outside the repository." },
  confirm_name: "Type the project's name exactly to confirm.",
  needs_confirmation: "Some agent files are tracked by git. Confirm them before WhyGraph changes them.",
  not_in_production: "This isn't available on a team portal.",
  hook_local_only: "Commit hooks trigger scans only on a local portal.",
  managed_on_platform: "This project is managed on the platform. Change it there.",

  // Linking to a platform (M2e)
  bad_client_name: "Use a machine name of 1-64 letters, digits, dots, underscores, dashes and spaces.",
  origin_mismatch:
    "This checkout's origin is not the platform project's repository, so it cannot be linked to it.",
  connect_expired: "The connection request expired. Start again from the local portal.",
  link_expired: "The link request expired. Start again from the platform.",
  connect_failed: "The platform didn't complete the connection. Start the link again.",
  issuer_mismatch: "The sign-in answer came from an unexpected server. Start the link again.",
  access_denied: "The connection was cancelled on the platform.",
  bad_platform_url: "That is not a valid platform address. Use the https address of your WhyGraph platform.",
  bad_platform_reply: "The platform sent an answer WhyGraph could not use. Check the address and try again.",
  bad_connect_request:
    "This connection request is not valid. Start it again from the local portal; do not edit the address.",
  platform_unreachable: { message: "Couldn't reach the platform. Check the address and try again.", action: RETRY },
  update_required: "This platform needs a newer WhyGraph on this machine. Update it, then link again.",
  invalid_grant: "This connection request has expired. Start the link again from your local portal.",
  invalid_token: "This connection token is not valid. Link the project again.",
  token_revoked: (err) => {
    const reason = str(err.extra.reason);
    return reason
      ? `This connection was revoked (${revokedLabel(reason).toLowerCase()}). Link the project again to reconnect.`
      : "This connection was revoked. Link the project again to reconnect.";
  },

  // Agents: the MCP / v1 data routes, Explorer and Chat
  bad_hunk: "The agent asked for a line range that could not be read.",
  bad_request: "WhyGraph couldn't use that request. Reload the page and try again.",
  body_too_large: "That request is too large.",
  no_evidence: {
    title: "No history yet",
    tone: "info",
    message: "This symbol has no history in the scanned commits, so there is nothing to explain yet.",
  },
  generation_not_permitted: "You can read rationale cards here but not generate new ones.",
  generation_limited: (err) => ({
    tone: "warn",
    message:
      err.extra.scope === "member"
        ? "You have reached your hourly limit for agent-triggered generations. Try again in a little while."
        : "This organization has reached its hourly limit for agent-triggered generations. Try again in a little while.",
  }),
  generation_disabled: (err) =>
    err.extra.scope === "member"
      ? "Agent-triggered generation is turned off for your account in this organization."
      : "Agent-triggered generation is turned off for this organization.",
  no_llm_key: (err, _ctx, mode) => {
    const provider = providerOf(err);
    const missing = provider ? `No ${provider} key.` : "No LLM key is set.";
    return mode === "production"
      ? { title: "No LLM key", message: `${missing} An owner can add one in Organization settings.` }
      : {
          title: "No LLM key",
          message: `${missing} Add one in Settings > Models and keys.`,
          action: { label: "Open Settings", to: "/settings" },
        };
  },
  llm_unavailable: { message: "The model provider couldn't answer. Try again in a moment.", action: RETRY },
  provider_error: { message: "The model provider returned an error. Try again; the details are below.", action: RETRY },
  bad_title: "Give the chat a name.",
  bad_provider: (_err, ctx) =>
    ctx === "usage" ? "Pick one of the listed providers." : "That provider isn't available here.",
  bad_model: (_err, ctx) => (ctx === "usage" ? "Enter the model id as the provider names it." : "Pick a model."),
  bad_content: "Write a message first.",
  bad_config: "This project's settings couldn't be read. Open Settings and save them again.",
  bad_target: "That symbol or line range can't be explained. Pick a symbol from the tree.",
  not_indexed: { title: "No code index yet", tone: "warn", message: "This project has no code index yet. Run a scan from its Overview." },
  symbol_not_found: "That symbol isn't in the code index any more. Search for it again.",
  blame_failed: "Couldn't read this file's history. It may have moved since the last scan.",

  // Usage & cost: budgets and prices (M2f-2)
  invalid_amount: "Enter a monthly budget greater than $0 and at most $1,000,000.",
  budget_above_org: (err) => {
    const org = err.extra.org_monthly_usd;
    return typeof org === "number"
      ? `A project or member budget must be at or below the organization's budget (${formatUsd(org)}).`
      : "A project or member budget must be at or below the organization's budget.";
  },
  budget_below_children: (err) => {
    const children = Array.isArray(err.extra.children) ? (err.extra.children as BudgetChild[]) : [];
    const names = children.map(childName).filter(Boolean);
    return names.length > 0
      ? `The organization's budget cannot be lower than these budgets: ${names.join(", ")}. Lower them first.`
      : "The organization's budget cannot be lower than a project or member budget. Lower those first.";
  },
  budget_exceeded: (err) => ({
    title: "Monthly budget reached",
    tone: "warn",
    message: budgetNoticeText(typeof err.extra.scope === "string" ? err.extra.scope : null),
  }),
  invalid_price: (err) =>
    typeof err.extra.field === "string"
      ? `${PRICE_FIELDS[err.extra.field] ?? err.extra.field} must be a price per million tokens from $0 to $10,000.`
      : "Enter input and output prices per million tokens, from $0 to $10,000.",
  bad_date: "Dates are written YYYY-MM-DD.",
  bad_range: "The start date must be before the end date.",
  range_too_long: "Pick a range of at most 400 days (usage is kept for 400 days).",
  bad_filter: "That filter isn't valid. Clear the filters and try again.",
  bad_group: "That breakdown isn't available.",
  bad_sort: "That sort order isn't available.",
  bad_cursor: "The list changed while paging. Apply the filter again.",
  usage_timeout: "That took too long to add up. Narrow the date range or add a filter.",
};

// ---- status fallbacks (an unknown code, or none) -------------------------------------

function statusTitle(status: number): string {
  if (status === 0) return "Can't reach WhyGraph";
  if (status === 401) return "Signed out";
  if (status === 403) return "No access";
  if (status === 404) return "Not found";
  if (status === 410) return "Expired";
  if (status === 413) return "Too large";
  if (status === 422) return "Check the details";
  if (status === 429) return "Too many attempts";
  if (status >= 400 && status < 500) return "Couldn't do that";
  return "Something went wrong";
}

/** The fixed sentence for a status, used when the code is unknown. */
function statusEntry(err: ApiError, mode: ErrorMode): Entry {
  const s = err.status;
  if (s === 0) return portalUnreachable(mode);
  if (s === 401) return { message: "Your session has ended. Sign in again." };
  if (s === 403) return forbidden(err, mode);
  if (s === 404) return { message: "That no longer exists." };
  if (s === 410) return { message: "This has expired. Start again." };
  if (s === 413) return { message: "That request is too large." };
  if (s === 429) return { tone: "warn", message: `Too many attempts. ${retryLine(err)}` };
  if (s === 409) return { message: "That can't be done right now. Reload the page and try again." };
  if (s >= 400 && s < 500) return { message: "WhyGraph couldn't use that request. Reload the page and try again." };
  return { message: GENERIC_5XX, action: RETRY };
}

/** The server's text as a sentence: a capital first letter and a full stop. */
function sentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return trimmed;
  const capital = trimmed[0].toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}

function build(code: string | null, status: number, entry: Entry, detail: string | null): ErrorInfo {
  return {
    code,
    title: entry.title ?? statusTitle(status),
    message: entry.message,
    tone: entry.tone ?? (status === 429 ? "warn" : "error"),
    ...(entry.action && { action: entry.action }),
    detail: detail && detail !== entry.message ? detail : null,
  };
}

const isAbort = (err: unknown) => (err as { name?: unknown } | null)?.name === "AbortError";

/** True when the registry has an entry for `code`. */
export function hasMessage(code: string): boolean {
  return Object.prototype.hasOwnProperty.call(MESSAGES, code);
}

/**
 * Everything a page needs to show a failure: a title, the human sentence, a tone,
 * an optional next step and the server's own text for "Show details".
 *
 * A known code reads from `MESSAGES` (some depend on `ctx`, the portal's mode or
 * the body's extras). A 4xx without a code shows the status title plus the
 * server's text, which is a sentence (§0.3 #45); an unknown code, a 401, a 429
 * and a 5xx get the fixed sentence for their status.
 */
export function errorInfo(err: unknown, ctx: ErrorContext = "generic"): ErrorInfo {
  const mode = getErrorMode();
  if (!(err instanceof ApiError)) {
    if (isAbort(err)) return { code: null, title: "Stopped", message: "Stopped.", tone: "info", detail: null };
    const detail = err instanceof Error ? err.message : typeof err === "string" ? err : null;
    return build(null, 500, { message: GENERIC_5XX, action: RETRY }, detail);
  }
  const detail = err.serverMessage || null;
  if (err.code && hasMessage(err.code)) {
    const resolver = MESSAGES[err.code];
    const resolved = typeof resolver === "function" ? resolver(err, ctx, mode) : resolver;
    return build(err.code, err.status, typeof resolved === "string" ? { message: resolved } : resolved, detail);
  }
  if (!err.code && err.status >= 400 && err.status < 500 && err.status !== 401 && err.status !== 429 && detail) {
    // A codeless refusal: the server words it.
    return build(`http_${err.status}`, err.status, { message: sentence(detail) }, null);
  }
  return build(err.code ?? `http_${err.status}`, err.status, statusEntry(err, mode), detail);
}

/** Just the sentence of {@link errorInfo}. */
export function errorMessage(err: unknown, ctx: ErrorContext = "generic"): string {
  return errorInfo(err, ctx).message;
}
