import { ApiError } from "../api";
import { formatUsd } from "./format";

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
  const who = child.name ?? child.label ?? (child.scope === "member_default" ? "the member default" : child.slug ?? child.uid);
  if (!who) return "";
  return typeof child.monthly_usd === "number" ? `${who} (${formatUsd(child.monthly_usd)})` : who;
}

const PRICE_FIELDS: Record<string, string> = {
  input_per_mtok: "The input price",
  output_per_mtok: "The output price",
  cache_read_per_mtok: "The cache read price",
  cache_write_per_mtok: "The cache write price",
};

export const PASSWORD_HINT = "At least 15 characters - a passphrase works well.";

/** A user-facing message for a failed credential call. */
export function authMessage(err: unknown): string {
  if (!(err instanceof ApiError)) return err instanceof Error ? err.message : "Something went wrong";
  switch (err.code) {
    case "weak_password":
      return "That password is too short. Use at least 15 characters - a passphrase works well.";
    case "common_password":
      return "That password is too common. Pick a longer, less guessable passphrase.";
    case "bad_email":
      return "Enter a valid email address.";
    case "email_taken": // the bootstrap form
      return "An account with this email already exists.";
    case "bad_credentials":
      return "Incorrect email or password.";
    case "bad_secret":
      return "That is not the bootstrap secret.";
    case "bad_token":
      return "This reset link is invalid, already used or expired. Ask an administrator for a new one.";
    // GitHub sign-in (M2d-1)
    case "oauth_state":
      return "The sign-in expired - try again.";
    case "github_auth_failed":
      return "GitHub did not accept the sign-in. Try again.";
    case "github_unavailable":
      return "GitHub could not be reached. Try again in a moment.";
    case "github_2fa_required":
      return "Your GitHub account needs two-factor authentication turned on before you can sign in to WhyGraph.";
    case "account_disabled":
      return "This account is disabled. Ask an instance administrator to enable it.";
    case "no_password":
      return "This account signs in with GitHub, so it has no password.";
    // Members and accounts (M2d-1)
    case "bad_login":
      return "That is not a GitHub username.";
    case "no_such_user":
      // The server's sentence is the instruction ("Ask them to sign in once, then add them.").
      return err.message;
    case "no_such_github_user":
    case "grants_for_org_admin":
    case "github_rate_limited":
      return err.message;
    case "already_invited":
      return "They already have an open invitation. Revoke it first to send a new one.";
    case "already_owner":
      return "They are already an owner.";
    case "org_admin":
      return "Org admins and owners already have every project role.";
    case "already_member":
      return "They are already a member of this organization.";
    case "user_disabled":
      return "This account is disabled. An instance administrator must enable it first.";
    case "last_owner":
      return "This is the organization's last owner. Make someone else an owner first.";
    case "not_member":
      return "That person is no longer a member of this organization.";
    case "owner_required":
      return "Only an owner can make someone an owner, or change or remove an owner.";
    case "self_disable":
      return "You cannot disable your own account.";
    case "last_admin":
      return "This is the last enabled instance administrator. Make someone else an administrator first.";
    // Projects from GitHub and org deletion (M2d-2)
    case "source_not_allowed":
      return "This kind of project is not supported here. Remove the project.";
    case "no_access":
      return "That repository is not available to you through this installation. Check that you can read it on GitHub and that the WhyGraph app covers it.";
    case "github_access_lost":
      return "WhyGraph can no longer read this repository on GitHub. Reconnect it on GitHub, then scan again.";
    case "github_authorization_required":
      return "Connect GitHub first: WhyGraph needs your authorization to list your repositories.";
    case "github_account_mismatch":
      return "You authorized a different GitHub account than the one you signed in with. Switch accounts on GitHub and try again.";
    case "github_required":
      return "Importing from GitHub needs an account that signs in with GitHub.";
    case "github_app_not_configured":
      return "This portal has no GitHub App configured. Ask an instance administrator.";
    case "start_from_portal":
      return "Open your organization in WhyGraph and choose Import from GitHub.";
    case "tracked_whygraph_state":
      return "This repository tracks WhyGraph's own state (.whygraph/ or .codegraph/). Remove it from the repository to import it.";
    case "confirm_slug":
      return "Type the organization's slug exactly to confirm.";
    case "busy":
      return "A sync is finishing - try again in a minute.";
    // Usage & cost: budgets and prices (M2f-2)
    case "invalid_amount":
      return "Enter a monthly budget greater than $0 and at most $1,000,000.";
    case "budget_above_org": {
      const org = err.extra.org_monthly_usd;
      return typeof org === "number"
        ? `A project or member budget must be at or below the organization's budget (${formatUsd(org)}).`
        : "A project or member budget must be at or below the organization's budget.";
    }
    case "budget_below_children": {
      const children = Array.isArray(err.extra.children) ? (err.extra.children as BudgetChild[]) : [];
      const names = children.map(childName).filter(Boolean);
      return names.length > 0
        ? `The organization's budget cannot be lower than these budgets: ${names.join(", ")}. Lower them first.`
        : "The organization's budget cannot be lower than a project or member budget. Lower those first.";
    }
    case "invalid_price":
      return typeof err.extra.field === "string"
        ? `${PRICE_FIELDS[err.extra.field] ?? err.extra.field} must be a price per million tokens from $0 to $10,000.`
        : "Enter input and output prices per million tokens, from $0 to $10,000.";
    case "bad_provider":
      return "Pick one of the listed providers.";
    case "bad_model":
      return "Enter the model id as the provider names it.";
    case "bad_date":
      return "Dates are written YYYY-MM-DD.";
    case "bad_range":
      return "The start date must be before the end date.";
    case "range_too_long":
      return "Pick a range of at most 400 days (usage is kept for 400 days).";
    case "bad_filter":
    case "bad_group":
    case "bad_sort":
      return err.message;
    case "bad_cursor":
      return "The list changed while paging. Apply the filter again.";
    case "usage_timeout":
      return "That took too long to add up. Narrow the date range or add a filter.";
  }
  if (err.status === 429) return "Too many attempts. Wait a few minutes and try again.";
  return err.message;
}
