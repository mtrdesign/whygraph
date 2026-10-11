import { errorMessage, type ErrorContext } from "./apiErrors";

export const PASSWORD_HINT = "At least 15 characters - a passphrase works well.";

/**
 * A user-facing message for a failed call: a thin wrapper over the error registry
 * (`lib/apiErrors.ts`), kept so existing call sites read the same. The `auth`
 * context words `bad_token` as a password-reset link.
 */
export function authMessage(err: unknown, ctx: ErrorContext = "auth"): string {
  return errorMessage(err, ctx);
}
