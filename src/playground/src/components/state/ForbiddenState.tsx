import { LockIcon } from "lucide-react";
import { ApiError, getErrorMode } from "../../api";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";

export type Grantor = "org-admin" | "project-admin" | "owner";

const GRANTED_BY: Record<Grantor, string> = {
  "org-admin": "An organization owner or admin can give you access.",
  "project-admin": "A project admin can give you access.",
  owner: "An organization owner can give you access.",
};

/**
 * A page or section the caller's role does not reach (production): what it is and
 * who can grant it. Local mode has one all-powerful user, so a local 403 is a bug
 * and renders as an `ErrorState` instead.
 */
export function ForbiddenState({
  what,
  grant = "org-admin",
  error,
}: {
  /** "the audit log", "this project's usage". */
  what: string;
  grant?: Grantor;
  /** The refusal, when there is one (shown as an error in local mode). */
  error?: unknown;
}) {
  if (getErrorMode() !== "production") {
    return <ErrorState error={error ?? new ApiError(403, `no access to ${what}`, "forbidden")} />;
  }
  return (
    <EmptyState
      icon={<LockIcon />}
      title="No access"
      description={`You don't have access to ${what}. ${GRANTED_BY[grant]}`}
    />
  );
}
