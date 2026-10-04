import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { ConfigForm } from "../components/portal/ConfigForm";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { canOwn, useReadOnly, useRole } from "../lib/identity";

type Cleared = { slug: string; provider: string }[];

/**
 * Screen 11: the defaults every project inherits (models, provider keys,
 * endpoints). The "no provider key set" banner comes from `GET defaults` inside
 * the form. Changing an endpoint clears the keys that were tied to the old one;
 * after a save this page lists the projects whose own key was cleared that way,
 * because they must enter it again.
 *
 * Only an owner may change them (`org.configure`); everyone else sees them read-only.
 */
export function GlobalSettingsPage() {
  const [cleared, setCleared] = useState<Cleared>([]);
  const role = useRole();
  const reader = useReadOnly();
  const editable = canOwn(role);
  return (
    <div className="mx-auto flex w-full max-w-[760px] flex-col gap-5 p-6 sm:p-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">Settings</h1>
        <p className="text-[13px] text-muted-foreground">
          Defaults for every project. A project can override any of these in its own settings.
        </p>
        {!editable && !reader && (
          <p className="text-[13px] text-muted-foreground" data-testid="settings-owner-only">
            Only an owner of this organization can change these settings.
          </p>
        )}
      </div>
      {cleared.length > 0 && (
        <Alert variant="destructive" data-testid="cleared-keys">
          <AlertTitle>Some project keys were cleared</AlertTitle>
          <AlertDescription>
            <p>
              The endpoint changed, so these projects' own keys for it were removed (a key must not
              follow a changed endpoint). Enter them again in each project's settings:
            </p>
            <ul className="mt-1 list-disc pl-4">
              {cleared.map((c) => (
                <li key={`${c.slug}:${c.provider}`}>
                  <Link
                    to="/p/$slug/settings"
                    params={{ slug: c.slug }}
                    className="text-primary-text hover:underline"
                  >
                    {c.slug}
                  </Link>{" "}
                  - <span className="font-mono">{c.provider}</span>
                </li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      <ConfigForm
        scope={{ kind: "global" }}
        submitLabel="Save"
        readOnly={!editable}
        onSaved={(saved) => setCleared(saved?.cleared_project_keys ?? [])}
      />
    </div>
  );
}
