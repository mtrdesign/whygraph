import type { ReactNode } from "react";
import type { AdminConfig, UpdateInfo } from "../../api";
import { formatBytes, timeAgo } from "../../lib/format";
import { errorLabel, sourceLabel } from "../../lib/updates";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";

const UPGRADING_URL = "https://mtrdesign.github.io/whygraph/deploy/production/#upgrading";

/** The base-URL self-check: `null` has not run yet, `[]` is clean. */
function AddressCheck({ checks }: { checks: string[] | null }) {
  if (checks === null) return <p className="text-sm text-muted-foreground">The address check has not run yet.</p>;
  if (checks.length === 0) {
    return (
      <Alert variant="success" data-testid="base-check-ok">
        <AlertDescription>The address check found no problems.</AlertDescription>
      </Alert>
    );
  }
  return (
    <Alert variant="destructive" data-testid="base-check">
      <AlertTitle>The address check found problems</AlertTitle>
      <AlertDescription>
        <ul className="list-disc pl-4">
          {checks.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  );
}

/** What this instance knows about newer releases (read-only; the check runs on the server). */
function UpdatesBlock({ update: u }: { update: UpdateInfo }) {
  const checked = timeAgo(u.checked_at);
  const why = sourceLabel(u.source);
  return (
    <div className="flex flex-col gap-1 text-sm" data-testid="admin-updates">
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
      <a
        href={UPGRADING_URL}
        target="_blank"
        rel="noreferrer noopener"
        className="text-primary-text hover:underline"
      >
        How to upgrade
      </a>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[11rem_1fr] sm:gap-4">
      <dt className="text-[13px] text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-sm">{children}</dd>
    </div>
  );
}

const mono = (value: string) => <span className="break-all font-mono text-[13px]">{value}</span>;

/** The Configuration page's read-only instance facts (secrets and `*_FILE` paths never reach the page). */
export function InstanceCard({ instance }: { instance: AdminConfig["instance"] }) {
  const { github_app: app, oauth_app: oauth, data_disk: disk } = instance;
  return (
    <dl className="flex flex-col gap-4" data-testid="admin-instance">
      <Row label="Address">{mono(instance.base_url)}</Row>
      <Row label="Address check">
        <AddressCheck checks={instance.base_check} />
      </Row>
      <Row label="Version and updates">
        <UpdatesBlock update={instance.update} />
      </Row>
      <Row label="GitHub App">
        {app ? (
          <>
            {mono(app.slug)} <span className="text-muted-foreground">client id</span> {mono(app.client_id)}
          </>
        ) : (
          <span className="text-muted-foreground">Not set</span>
        )}
      </Row>
      <Row label="OAuth App client id">
        {oauth ? mono(oauth.client_id) : <span className="text-muted-foreground">Not set</span>}
      </Row>
      <Row label="Data volume free">
        {disk ? (
          `${formatBytes(disk.free)} of ${formatBytes(disk.total)}; the database is on its own volume`
        ) : (
          <span className="text-muted-foreground">Unknown</span>
        )}
      </Row>
      <Row label="Database size">
        {instance.postgres_bytes === null ? (
          <span className="text-muted-foreground">Unknown</span>
        ) : (
          formatBytes(instance.postgres_bytes)
        )}
      </Row>
    </dl>
  );
}
