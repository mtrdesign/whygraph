import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, accountApi, portalKey } from "../api";
import { UserAvatar } from "../components/auth/UserAvatar";
import { MyConnectedPortals } from "../components/portal/ConnectedPortals";
import { SpendBar, UsageSection } from "../components/usage/parts";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { PASSWORD_HINT, authMessage } from "../lib/authErrors";
import { formatUsd } from "../lib/format";
import { useSignOut } from "../lib/identity";
import { formatResetsAt } from "../lib/usageRange";
import { safeHref } from "../lib/platformLink";

/** My usage (production): what the caller spent this month in each org, against their budget there. */
function MyUsage() {
  const usage = useQuery({ queryKey: ["@account", "usage"], queryFn: accountApi.usage });
  const orgs = usage.data?.orgs ?? [];
  if (usage.isLoading || usage.isError || orgs.length === 0) return null;
  return (
    <UsageSection
      title="My usage"
      description={`Estimated LLM spend this month. Resets ${formatResetsAt(usage.data?.resets_at ?? "")}.`}
      testId="account-usage"
    >
      <ul className="divide-y divide-border">
        {orgs.map((o) => {
          const href = safeHref(`${o.url.replace(/\/$/, "")}/usage/me`);
          return (
            <li key={o.slug} className="flex flex-col gap-1.5 py-3" data-testid={`account-usage-${o.slug}`}>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-medium">{o.name}</span>
                <span className="text-sm tabular-nums">
                  {formatUsd(o.spent_usd)}
                  {o.budget_usd !== null && (
                    <span className="text-muted-foreground"> of {formatUsd(o.budget_usd)}</span>
                  )}
                </span>
              </div>
              <SpendBar pct={o.pct} label={`${o.name} budget used`} />
              <div className="flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
                <span>
                  {o.calls.toLocaleString("en-US")} {o.calls === 1 ? "call" : "calls"}
                </span>
                {o.hard_stop && o.budget_usd !== null && <span>Hard stop on</span>}
                {href && (
                  <a href={href} className="text-primary-text hover:underline">
                    Open my usage
                  </a>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </UsageSection>
  );
}

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

      <MyUsage />

      <MyConnectedPortals />

      <div>
        <Button variant="outline" onClick={() => void signOut()}>
          Sign out
        </Button>
      </div>
    </div>
  );
}
