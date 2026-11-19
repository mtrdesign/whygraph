import { useState, type FormEvent } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, adminApi, type AdminUser } from "../../api";
import { errorMessage } from "../../lib/apiErrors";
import { baseHostOf, usePortalState } from "../../lib/identity";
import { Field } from "../portal/Field";
import { Alert, AlertDescription } from "../ui/alert";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { Textarea } from "../ui/textarea";
import { ADMIN } from "./adminParts";

/** The user a GitHub username names, from the cached users list: case-insensitive, a leading `@` stripped, deleted and disabled users left out. */
export function findByLogin(users: AdminUser[] | undefined, login: string): AdminUser | undefined {
  const wanted = login.trim().replace(/^@/, "").toLowerCase();
  if (!wanted) return undefined;
  return users?.find((u) => !u.deleted_at && !u.disabled && u.github_login?.toLowerCase() === wanted);
}

export const NO_SUCH_ACCOUNT =
  "No WhyGraph account has that GitHub username. They must sign in with GitHub once, or an owner can invite them.";

/** A starting URL name from the display name; the admin can edit it (as `CreateOrgPage` does). */
function suggestSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/, "");
}

/**
 * Create organization (plan 59, §4.7): name, URL name (prefilled from the name) and the owner's
 * GitHub username, resolved against the users list. There is no live slug check (the everyday
 * route reads the everyday session), so `slug_taken` and `bad_slug` come back from the submit.
 */
export function CreateOrgDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const host = baseHostOf(usePortalState().data?.base_url);
  const users = useQuery({ queryKey: [...ADMIN, "users"], queryFn: adminApi.users, enabled: open });
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [login, setLogin] = useState("");
  const [ownerError, setOwnerError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: (ownerUid: string) => adminApi.createOrg({ slug, name: name.trim(), owner_uid: ownerUid }),
    onSuccess: async ({ slug: created }) => {
      await queryClient.invalidateQueries({ queryKey: ADMIN });
      change(false);
      await navigate({ to: "/admin/orgs/$slug", params: { slug: created } });
    },
  });
  const code = create.error instanceof ApiError ? create.error.code : undefined;
  const slugError =
    code === "slug_taken" ? "That URL name is already taken." : code === "bad_slug" ? errorMessage(create.error) : undefined;

  const change = (next: boolean) => {
    if (!next) {
      setName("");
      setSlug("");
      setSlugEdited(false);
      setLogin("");
      setOwnerError(null);
      create.reset();
    }
    onOpenChange(next);
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const owner = findByLogin(users.data, login);
    if (!owner) {
      setOwnerError(NO_SUCH_ACCOUNT);
      return;
    }
    setOwnerError(null);
    create.mutate(owner.uid);
  };
  const generic = create.isError && !slugError ? errorMessage(create.error) : null;

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogContent data-testid="create-org-dialog">
        <form noValidate onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>Create organization</DialogTitle>
            <DialogDescription>The owner you name gets the organization at once.</DialogDescription>
          </DialogHeader>
          <Field label="Organization name">
            {(p) => (
              <Input
                {...p}
                autoFocus
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  if (!slugEdited) setSlug(suggestSlug(e.target.value));
                }}
              />
            )}
          </Field>
          <Field
            label="URL name"
            hint={slug ? `Its address will be ${slug}.${host}` : undefined}
            error={slugError}
          >
            {(p) => (
              <Input
                {...p}
                autoComplete="off"
                spellCheck={false}
                value={slug}
                onChange={(e) => {
                  setSlug(e.target.value);
                  setSlugEdited(true);
                }}
              />
            )}
          </Field>
          <Field
            label="Owner's GitHub username"
            hint="The owner must have signed in to WhyGraph with GitHub once."
            error={ownerError ?? undefined}
          >
            {(p) => (
              <Input
                {...p}
                autoComplete="off"
                spellCheck={false}
                placeholder="octocat"
                value={login}
                onChange={(e) => setLogin(e.target.value)}
              />
            )}
          </Field>
          {generic && (
            <Alert variant="destructive">
              <AlertDescription>{generic}</AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => change(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !name.trim() || !slug || !login.trim()}>
              Create organization
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** "Rename": the "Organization name" field, `bad_name` as its error. */
export function RenameOrgDialog({
  slug,
  name,
  open,
  onOpenChange,
}: {
  slug: string;
  name: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState(name);
  const rename = useMutation({
    mutationFn: () => adminApi.renameOrg(slug, value.trim()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ADMIN });
      onOpenChange(false);
    },
  });
  const code = rename.error instanceof ApiError ? rename.error.code : undefined;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (next) {
          setValue(name);
          rename.reset();
        }
        onOpenChange(next);
      }}
    >
      <DialogContent data-testid="rename-org-dialog">
        <form
          noValidate
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            rename.mutate();
          }}
        >
          <DialogHeader>
            <DialogTitle>Rename {name}</DialogTitle>
            <DialogDescription>The URL name {slug} does not change.</DialogDescription>
          </DialogHeader>
          <Field label="Organization name" error={code === "bad_name" ? errorMessage(rename.error) : undefined}>
            {(p) => <Input {...p} autoFocus value={value} onChange={(e) => setValue(e.target.value)} />}
          </Field>
          {rename.isError && code !== "bad_name" && (
            <Alert variant="destructive">
              <AlertDescription>{errorMessage(rename.error)}</AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={rename.isPending || !value.trim() || value.trim() === name}>
              Rename
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** "Suspend": a reason of 1-500 characters that members see; the button stays disabled while it is blank. */
export function SuspendOrgDialog({
  slug,
  name,
  open,
  onOpenChange,
}: {
  slug: string;
  name: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const suspend = useMutation({
    mutationFn: () => adminApi.suspendOrg(slug, reason.trim()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ADMIN });
      onOpenChange(false);
    },
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setReason("");
          suspend.reset();
        }
        onOpenChange(next);
      }}
    >
      <DialogContent data-testid="suspend-org-dialog">
        <DialogHeader>
          <DialogTitle>Suspend {name}?</DialogTitle>
          <DialogDescription>
            Members keep read access. Changes, scans, chat and agent calls stop until you unsuspend, and running
            scans are cancelled. Members see your reason.
          </DialogDescription>
        </DialogHeader>
        <Field label="Reason (members see it)">
          {(p) => <Textarea {...p} maxLength={500} rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />}
        </Field>
        {suspend.isError && (
          <Alert variant="destructive">
            <AlertDescription>{errorMessage(suspend.error)}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={suspend.isPending || reason.trim() === ""} onClick={() => suspend.mutate()}>
            Suspend
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
