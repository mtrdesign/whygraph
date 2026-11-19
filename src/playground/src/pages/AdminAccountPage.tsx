import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, adminApi, type AdminReaderGrant } from "../api";
import { toastError } from "../components/admin/adminParts";
import { UserAvatar } from "../components/auth/UserAvatar";
import { PageContainer } from "../components/layout/PageContainer";
import { Field } from "../components/portal/Field";
import { QueryState } from "../components/state/QueryState";
import { FormSkeleton } from "../components/state/Skeletons";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { PASSWORD_HINT, authMessage } from "../lib/authErrors";
import { formatDateTime } from "../lib/format";

/** The password form of a password account, moved from the everyday Account page. */
function PasswordForm() {
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const password = useMutation({
    mutationFn: () => adminApi.changePassword({ current, new: next }),
    onSuccess: async () => {
      toast.success("Password changed. Your other sessions were signed out.");
      setCurrent("");
      setNext("");
      setConfirm("");
      // This session was replaced by a new one: its end moved.
      await queryClient.invalidateQueries({ queryKey: ["@admin"] });
    },
  });
  const code = password.error instanceof ApiError ? password.error.code : undefined;
  const mismatch = confirm !== "" && confirm !== next ? "The passwords do not match." : undefined;
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    password.mutate();
  };
  return (
    <form
      noValidate
      onSubmit={onSubmit}
      className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5 shadow-card"
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
  );
}

/** Live per-org reader grants with Revoke (plan 59, §4.4): the read-only access this administrator opened. */
function ReaderGrants({ grants }: { grants: AdminReaderGrant[] }) {
  const queryClient = useQueryClient();
  const revoke = useMutation({
    mutationFn: (id: number) => adminApi.revokeReaderGrant(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["@admin"] }),
    onError: toastError,
  });
  return (
    <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card" data-testid="reader-grants">
      <h2 className="text-sm font-semibold">Read-only access</h2>
      {grants.length === 0 ? (
        <p className="text-sm text-muted-foreground">No open read-only access.</p>
      ) : (
        <ul className="divide-y divide-border">
          {grants.map((g) => (
            <li key={g.id} className="row-wrap py-2" data-testid={`reader-grant-${g.org_slug}`}>
              <span className="flex min-w-0 flex-1 flex-col leading-tight">
                <a
                  href={g.org_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="truncate font-medium text-primary-text hover:underline"
                >
                  {g.org_name}
                </a>
                <span className="text-xs text-muted-foreground">Until {formatDateTime(g.expires_at)}</span>
              </span>
              <Button size="sm" variant="outline" disabled={revoke.isPending} onClick={() => revoke.mutate(g.id)}>
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * `/admin/account` (D48): this admin session's start and end, and for a password
 * account the password form; the live reader grants come between, with Revoke.
 */
export function AdminAccountPage() {
  const me = useQuery({ queryKey: ["@admin", "me"], queryFn: adminApi.me });
  return (
    <PageContainer width="narrow" className="flex flex-col gap-6">
      <div className="flex items-center gap-3">
        {me.data && <UserAvatar name={me.data.display_name} url={me.data.avatar_url} className="size-10 text-sm" />}
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">Account</h1>
          {me.data && (
            <p className="text-[13px] text-muted-foreground" data-testid="admin-identity">
              {me.data.display_name}
              {me.data.github_login ? <span className="font-mono"> @{me.data.github_login}</span> : null}
            </p>
          )}
        </div>
      </div>
      <QueryState query={me} loading={<FormSkeleton />} errorTitle="Couldn't load your account">
        {(m) => (
          <>
            <section
              className="flex flex-col gap-2 rounded-xl border border-border bg-card p-5 shadow-card"
              data-testid="admin-session"
            >
              <h2 className="text-sm font-semibold">This session</h2>
              <dl className="flex flex-col gap-1 text-sm">
                <div>Signed in {formatDateTime(m.session.created_at)}</div>
                <div>Ends {formatDateTime(m.session.expires_at)}</div>
              </dl>
              <p className="text-xs text-muted-foreground">
                Administration sessions last 8 hours and end when you sign out.
              </p>
            </section>
            <ReaderGrants grants={m.reader_grants} />
            {m.has_password && <PasswordForm />}
          </>
        )}
      </QueryState>
    </PageContainer>
  );
}
