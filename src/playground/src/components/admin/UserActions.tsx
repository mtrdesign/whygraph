import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { adminApi, type AdminUser } from "../../api";
import { ConfirmDialog } from "../portal/ConfirmDialog";
import { CopyButton } from "../portal/CopyButton";
import { Button } from "../ui/button";
import { authMessage } from "../../lib/authErrors";

// Every admin mutation invalidates the whole `["@admin"]` prefix (plan 59, §4.15).
const ADMIN = ["@admin"] as const;

/**
 * One user's actions (`isMe` comes from `/api/admin/me`, not the everyday session): Make / Remove admin (secondary), Disable (a destructive
 * outline, after a confirmation) / Enable and, for a password account, a one-time
 * reset link (shown once, to copy).
 */
export function UserActions({ user, isMe }: { user: AdminUser; isMe: boolean }) {
  const queryClient = useQueryClient();
  const [link, setLink] = useState<string | null>(null);
  const [disabling, setDisabling] = useState(false);
  const setAdmin = useMutation({
    mutationFn: (value: boolean) => adminApi.setAdmin(user.uid, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ADMIN }),
    onError: (err) => toast.error(authMessage(err)),
  });
  const setDisabled = useMutation({
    mutationFn: (value: boolean) => adminApi.setDisabled(user.uid, value),
    onSuccess: async () => {
      setDisabling(false);
      await queryClient.invalidateQueries({ queryKey: ADMIN });
    },
  });
  const reset = useMutation({
    mutationFn: () => adminApi.resetLink(user.uid),
    onSuccess: ({ url }) => setLink(url),
    onError: (err) => toast.error(authMessage(err)),
  });
  const name = user.display_name;
  return (
    <div className="flex flex-col gap-2">
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
            <Button
              size="sm"
              variant="outline"
              disabled={setDisabled.isPending}
              onClick={() => setDisabled.mutate(false)}
            >
              Enable
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              className="border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive"
              onClick={() => {
                setDisabled.reset();
                setDisabling(true);
              }}
            >
              Disable
            </Button>
          ))}
        {user.has_password && !user.disabled && (
          <Button size="sm" variant="outline" disabled={reset.isPending} onClick={() => reset.mutate()}>
            Copy reset link
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
        error={setDisabled.isError ? authMessage(setDisabled.error) : null}
        onConfirm={() => setDisabled.mutate(true)}
      />
    </div>
  );
}
