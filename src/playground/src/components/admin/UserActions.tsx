import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, adminApi, type AdminUser } from "../../api";
import { errorMessage } from "../../lib/apiErrors";
import { plural } from "../../lib/plural";
import { ConfirmDialog } from "../portal/ConfirmDialog";
import { CopyButton } from "../portal/CopyButton";
import { TypedConfirmDialog } from "../portal/TypedConfirmDialog";
import { Button } from "../ui/button";
import { ADMIN, toastError, userConfirmValue } from "./adminParts";

const DESTRUCTIVE = "border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive";

/** What a typed confirmation asks for, so the admin knows which of the three to type. */
function typedHint(user: AdminUser): string {
  return user.github_login
    ? "Type their GitHub login without the @."
    : user.email
      ? "Type their email address."
      : "Type their ID.";
}

/**
 * One user's actions on the detail page (`isMe` comes from `/api/admin/me`, not the everyday session).
 * A live account: Make / Remove admin (secondary), Disable (a destructive outline, after a confirmation) /
 * Enable, a reset link for a password administrator (shown once, to copy), and Delete (typed). A deleted
 * account has only Restore and Erase permanently (typed); an erased one has no actions (D34, §4.15).
 */
export function UserActions({ user, isMe }: { user: AdminUser; isMe: boolean }) {
  const queryClient = useQueryClient();
  const [link, setLink] = useState<string | null>(null);
  const [disabling, setDisabling] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [erasing, setErasing] = useState(false);
  const done = () => queryClient.invalidateQueries({ queryKey: ADMIN });
  const name = user.display_name;
  const expected = userConfirmValue(user);

  const setAdmin = useMutation({
    mutationFn: (value: boolean) => adminApi.setAdmin(user.uid, value),
    onSuccess: done,
    onError: toastError,
  });
  const setDisabled = useMutation({
    mutationFn: (value: boolean) => adminApi.setDisabled(user.uid, value),
    onSuccess: async () => {
      setDisabling(false);
      await done();
    },
  });
  const reset = useMutation({
    mutationFn: () => adminApi.resetLink(user.uid),
    onSuccess: ({ url }) => setLink(url),
    onError: toastError,
  });
  const enable = useMutation({
    mutationFn: () => adminApi.setDisabled(user.uid, false),
    onSuccess: done,
    onError: toastError,
  });
  const remove = useMutation({
    mutationFn: () => adminApi.deleteUser(user.uid, expected),
    onSuccess: async () => {
      setDeleting(false);
      await done();
    },
  });
  const restore = useMutation({
    mutationFn: () => adminApi.restoreUser(user.uid),
    onSuccess: async ({ restored, skipped }) => {
      const back = restored.map((r) => r.org).join(", ") || "none";
      toast.success(`${name} restored`, {
        description:
          `Back in: ${back}.` +
          (skipped.length > 0 ? ` Skipped (organization deleted permanently): ${skipped.length}.` : ""),
      });
      await done();
    },
    onError: toastError,
  });
  const erase = useMutation({
    mutationFn: () => adminApi.eraseUser(user.uid, expected),
    onSuccess: async () => {
      setErasing(false);
      await done();
    },
  });
  // `busy` on Erase means recent events are still being written (§4.16): the call site words it.
  const eraseError = erase.isError
    ? erase.error instanceof ApiError && erase.error.code === "busy"
      ? "WhyGraph is still writing recent events. Try Erase again in a minute."
      : errorMessage(erase.error)
    : null;

  if (user.erased) return null;

  if (user.deleted_at) {
    return (
      <div className="flex flex-wrap items-center gap-2" data-testid="user-actions">
        <Button size="sm" variant="secondary" disabled={restore.isPending} onClick={() => restore.mutate()}>
          Restore
        </Button>
        <Button
          size="sm"
          variant="outline"
          className={DESTRUCTIVE}
          onClick={() => {
            erase.reset();
            setErasing(true);
          }}
        >
          Erase permanently
        </Button>
        <TypedConfirmDialog
          open={erasing}
          onOpenChange={setErasing}
          testId="erase-user-dialog"
          title={`Erase ${name} permanently?`}
          description={
            <>
              This cannot be undone. Their name, GitHub login, email, avatar, IP addresses, machine names,
              invitations and chats are removed. History keeps an entry named [Deleted User]. Log lines, backups
              and files already exported are not changed. The GitHub account can sign up again as a new account.{" "}
              {typedHint(user)}
            </>
          }
          expected={expected}
          confirmLabel="Erase permanently"
          pendingLabel="Erasing..."
          pending={erase.isPending}
          error={eraseError}
          onConfirm={() => erase.mutate()}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="user-actions">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="secondary"
          disabled={setAdmin.isPending}
          onClick={() => setAdmin.mutate(!user.is_instance_admin)}
        >
          {user.is_instance_admin ? "Remove admin" : "Make admin"}
        </Button>
        {!isMe &&
          (user.disabled ? (
            <Button size="sm" variant="outline" disabled={enable.isPending} onClick={() => enable.mutate()}>
              Enable
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              className={DESTRUCTIVE}
              onClick={() => {
                setDisabled.reset();
                setDisabling(true);
              }}
            >
              Disable
            </Button>
          ))}
        {user.has_password && user.is_instance_admin && !user.disabled && (
          <Button size="sm" variant="outline" disabled={reset.isPending} onClick={() => reset.mutate()}>
            Copy reset link
          </Button>
        )}
        {!isMe && (
          <Button
            size="sm"
            variant="outline"
            className={DESTRUCTIVE}
            onClick={() => {
              remove.reset();
              setDeleting(true);
            }}
          >
            Delete
          </Button>
        )}
      </div>
      {link && (
        <div className="flex flex-wrap items-center gap-2" data-testid="reset-link">
          <code className="min-w-0 flex-1 overflow-x-auto rounded-md bg-muted px-2 py-1.5 font-mono text-xs">
            {link}
          </code>
          <CopyButton text={link} />
          <span className="text-xs text-muted-foreground">One use, valid for 24 hours.</span>
        </div>
      )}
      <ConfirmDialog
        open={disabling}
        onOpenChange={setDisabling}
        title={`Disable ${name}?`}
        description={`${name}'s sessions end at once and they can't sign in until you enable the account. Memberships are kept.`}
        confirmLabel="Disable"
        pending={setDisabled.isPending}
        error={setDisabled.isError ? errorMessage(setDisabled.error) : null}
        onConfirm={() => setDisabled.mutate(true)}
      />
      <TypedConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        testId="delete-user-dialog"
        title={`Delete ${name}?`}
        description={
          <>
            {name} is signed out everywhere, their connection tokens are revoked and they leave{" "}
            {plural(user.org_count, "organization")}. Organization pages show them as [Deleted User]. You can
            restore the account; their admin role, project access and member budgets do not come back.{" "}
            {typedHint(user)}
          </>
        }
        expected={expected}
        confirmLabel="Delete user"
        pendingLabel="Deleting..."
        pending={remove.isPending}
        error={remove.isError ? errorMessage(remove.error) : null}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}
