import { useQuery } from "@tanstack/react-query";
import { adminApi } from "../../api";
import { ErrorState } from "../state/ErrorState";
import { Skeleton } from "../ui/skeleton";
import { timeAgo } from "../../lib/format";
import { errorLabel, sourceLabel } from "../../lib/updates";

const UPGRADING_URL = "https://mtrdesign.github.io/whygraph/deploy/production/#upgrading";

/** What this instance knows about newer releases (read-only; the check runs on the server). */
export function UpdatesCard() {
  const update = useQuery({ queryKey: ["@admin", "update"], queryFn: adminApi.update });
  const u = update.data;
  const checked = u ? timeAgo(u.checked_at) : null;
  const why = u ? sourceLabel(u.source) : "";
  return (
    <section
      className="flex flex-col gap-2 rounded-xl border border-border bg-card p-5 shadow-card"
      data-testid="admin-updates"
    >
      <h2 className="text-sm font-semibold">Updates</h2>
      {update.isLoading && <Skeleton className="h-10" />}
      {update.isError && (
        <ErrorState error={update.error} title="Couldn't load the update status" onRetry={() => void update.refetch()} />
      )}
      {u && (
        <dl className="flex flex-col gap-1 text-sm">
          <div>Current version: {u.current ?? "unknown"}</div>
          <div data-testid="admin-updates-latest">
            {u.available && u.latest
              ? `Latest version: ${u.latest}`
              : // "Up to date" only from a check that ran and succeeded with the check on.
                u.enabled && u.checked_at && !u.error
                ? "Up to date"
                : "Latest version: unknown"}
          </div>
          {u.available && u.manual_steps && (
            <div className="font-medium text-warning" data-testid="admin-updates-manual">
              This release needs manual steps - read its notes and the upgrade steps first.
            </div>
          )}
          {u.available && u.notes_url && (
            <div>
              <a
                href={u.notes_url}
                target="_blank"
                rel="noreferrer noopener"
                className="text-primary-text hover:underline"
              >
                What's new in {u.latest}
              </a>
            </div>
          )}
          <div>
            Update check: {u.enabled === null ? "not run yet" : u.enabled ? "on" : "off"}
            {why && ` (${why})`}
          </div>
          <div>
            {u.error
              ? `Last check failed: ${errorLabel(u.error)}`
              : checked
                ? `Last checked ${checked}`
                : "Not checked yet"}
          </div>
        </dl>
      )}
      <a
        href={UPGRADING_URL}
        target="_blank"
        rel="noreferrer noopener"
        className="text-sm text-primary-text hover:underline"
      >
        How to upgrade
      </a>
    </section>
  );
}
