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
    case "email_taken":
      return "An account with this email already exists.";
    case "bad_credentials":
      return "Incorrect email or password.";
    case "bad_secret":
      return "That is not the bootstrap secret.";
    case "bad_token":
      return "This reset link is invalid, already used or expired. Ask an administrator for a new one.";
  }
  if (err.status === 429) return "Too many attempts. Wait a few minutes and try again.";
  return err.message;
}
