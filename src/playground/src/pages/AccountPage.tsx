import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, accountApi, portalKey } from "../api";
import { UserAvatar } from "../components/auth/UserAvatar";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { PASSWORD_HINT, authMessage } from "../lib/authErrors";
import { useSignOut } from "../lib/identity";

/**
 * `/account`: display name, password and sign out. Lives on the base host. A
 * GitHub account shows its avatar and `@login` and has no password section.
 */
export function AccountPage() {
  const queryClient = useQueryClient();
  const signOut = useSignOut();
  const account = useQuery({ queryKey: ["@account", "me"], queryFn: accountApi.get });
  const [name, setName] = useState("");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");

  useEffect(() => {
    if (account.data) setName(account.data.display_name);
  }, [account.data]);

  const rename = useMutation({
    mutationFn: () => accountApi.update(name.trim()),
    onSuccess: async () => {
      toast.success("Name updated");
      await queryClient.invalidateQueries({ queryKey: ["@account", "me"] });
      await queryClient.invalidateQueries({ queryKey: portalKey("state") });
    },
    onError: (err) => toast.error(err.message),
  });
  const password = useMutation({
    mutationFn: () => accountApi.password({ current, new: next }),
    onSuccess: () => {
      toast.success("Password changed. Your other sessions were signed out.");
      setCurrent("");
      setNext("");
      setConfirm("");
    },
  });
  const code = password.error instanceof ApiError ? password.error.code : undefined;
  const mismatch = confirm !== "" && confirm !== next ? "The passwords do not match." : undefined;

  const onRename = (e: FormEvent) => {
    e.preventDefault();
    rename.mutate();
  };
  const onPassword = (e: FormEvent) => {
    e.preventDefault();
    password.mutate();
  };

  return (
    <div className="mx-auto flex w-full max-w-xl flex-col gap-6 p-6 sm:p-8">
      <div className="flex items-center gap-3">
        {account.data && (
          <UserAvatar name={account.data.display_name} url={account.data.avatar_url} className="size-10 text-sm" />
        )}
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">Account</h1>
          {account.data && (
            <p className="text-[13px] text-muted-foreground" data-testid="account-identity">
              {account.data.github_login ? (
                <span className="font-mono">@{account.data.github_login}</span>
              ) : null}
              {account.data.github_login && account.data.email ? " - " : null}
              {account.data.email}
            </p>
          )}
        </div>
      </div>

      <form noValidate onSubmit={onRename} className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
        <h2 className="text-sm font-semibold">Profile</h2>
        <Field label="Display name">
          {(p) => <Input {...p} value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <div>
          <Button
            type="submit"
            disabled={rename.isPending || !name.trim() || name.trim() === account.data?.display_name}
          >
            Save name
          </Button>
        </div>
      </form>

      {account.data?.has_password && (
        <form
          noValidate
          onSubmit={onPassword}
          className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5"
        >
          <h2 className="text-sm font-semibold">Change password</h2>
          <Field
            label="Current password"
            error={code === "bad_credentials" ? "That is not your current password." : undefined}
          >
            {(p) => (
              <Input
                {...p}
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
            )}
          </Field>
          <Field
            label="New password"
            hint={PASSWORD_HINT}
            error={code === "weak_password" || code === "common_password" ? authMessage(password.error) : undefined}
          >
            {(p) => (
              <Input
                {...p}
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
            )}
          </Field>
          <Field label="Confirm new password" error={mismatch}>
            {(p) => (
              <Input
                {...p}
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            )}
          </Field>
          {password.isError &&
            code !== "bad_credentials" &&
            code !== "weak_password" &&
            code !== "common_password" && (
              <Alert variant="destructive">
                <AlertDescription>{authMessage(password.error)}</AlertDescription>
              </Alert>
            )}
          <div>
            <Button type="submit" disabled={password.isPending || !current || !next || next !== confirm}>
              Change password
            </Button>
          </div>
        </form>
      )}

      <div>
        <Button variant="outline" onClick={() => void signOut()}>
          Sign out
        </Button>
      </div>
    </div>
  );
}
