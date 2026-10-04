import { ApiError } from "../api";

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
  }
  if (err.status === 429) return "Too many attempts. Wait a few minutes and try again.";
  return err.message;
}
