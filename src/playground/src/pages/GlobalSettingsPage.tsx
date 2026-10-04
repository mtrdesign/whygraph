import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { orgsApi, portalApi, portalKey } from "../api";
import { ConfigForm } from "../components/portal/ConfigForm";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog";
import { Input } from "../components/ui/input";
import { authMessage } from "../lib/authErrors";
import { canOwn, isProduction, usePortalState, useReadOnly, useRole } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";

type Cleared = { slug: string; provider: string }[];

/**
 * "Delete organization" (production, owners only; M2d-2 plan section 4.8):
 * immediate and final, confirmed by typing the org's slug. Running scans are
 * cancelled; a sync still fetching makes the server answer `409 busy`, and the
 * owner tries again. Afterwards the owner lands on the base host's org picker.
 */
function DeleteOrganization({ slug, name, baseUrl }: { slug: string; name: string; baseUrl: string }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  const count = projects.data?.projects.length;
  const remove = useMutation({
    mutationFn: () => orgsApi.remove(typed),
    onSuccess: () => void hardNavigate(`${new URL(baseUrl).origin}/orgs`),
  });
  const close = (o: boolean) => {
    setOpen(o);
    if (!o) {
      setTyped("");
      remove.reset();
    }
  };

  return (
    <section
      aria-label="Danger zone"
      className="flex flex-col gap-4 rounded-xl border border-destructive/40 bg-card p-5"
      data-testid="org-danger-zone"
    >
      <div>
        <h2 className="text-sm font-semibold text-destructive">Danger zone</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Deletes this organization with its projects, scans, members' access, settings and keys. There
          is no undo, and the name <span className="font-mono">{slug}</span> cannot be used again.
        </p>
      </div>
      <div>
        <Button variant="destructive" onClick={() => setOpen(true)}>
          Delete organization
        </Button>
      </div>
      <Dialog open={open} onOpenChange={close}>
        <DialogContent data-testid="delete-org-dialog">
          <DialogHeader>
            <DialogTitle>Delete {name}?</DialogTitle>
            <DialogDescription>
              {count === undefined
                ? "Every project of this organization is deleted with it."
                : count === 1
                  ? "Its 1 project is deleted with it."
                  : `Its ${count} projects are deleted with it.`}{" "}
              Running scans are cancelled. The repositories on GitHub are not changed.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="confirm-org-slug" className="text-sm">
              Type <span className="font-mono font-medium">{slug}</span> to confirm
            </label>
            <Input
              id="confirm-org-slug"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
            />
          </div>
          {remove.isError && (
            <Alert variant="destructive" data-testid="delete-org-error">
              <AlertDescription>{authMessage(remove.error)}</AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => close(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={typed !== slug || remove.isPending}
              onClick={() => remove.mutate()}
            >
              {remove.isPending ? "Deleting…" : "Delete organization"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

/**
 * Screen 11: the defaults every project inherits (models, provider keys,
 * endpoints). The "no provider key set" banner comes from `GET defaults` inside
 * the form. Changing an endpoint clears the keys that were tied to the old one;
 * after a save this page lists the projects whose own key was cleared that way,
 * because they must enter it again.
 *
 * Only an owner may change them (`org.configure`); everyone else sees them read-only.
 * In production an owner also gets the Delete organization danger zone.
 */
export function GlobalSettingsPage() {
  const [cleared, setCleared] = useState<Cleared>([]);
  const role = useRole();
  const reader = useReadOnly();
  const editable = canOwn(role);
  const state = usePortalState().data;
  const org = state?.org;
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
      {isProduction(state) && editable && org && state?.base_url && (
        <DeleteOrganization slug={org.slug} name={org.name} baseUrl={state.base_url} />
      )}
    </div>
  );
}
