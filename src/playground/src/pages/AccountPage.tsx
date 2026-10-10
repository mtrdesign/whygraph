import { PageContainer } from "../components/layout/PageContainer";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, accountApi, portalKey } from "../api";
import { ACCOUNT_ORGS_KEY } from "../components/shell/OrgSwitcher";
import { ErrorState } from "../components/state/ErrorState";
import { StatusPill } from "../components/ui/status-pill";
import { Badge } from "../components/ui/badge";
import { orgRoleLabel } from "../lib/labels";
import { UserAvatar } from "../components/auth/UserAvatar";
import { MyConnectedPortals } from "../components/portal/ConnectedPortals";
import { SpendBar, UsageSection } from "../components/usage/parts";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";
import { Input } from "../components/ui/input";
import { PASSWORD_HINT, authMessage } from "../lib/authErrors";
import { formatNumber, formatUsd } from "../lib/format";
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
          const stopped = o.hard_stop && o.budget_usd !== null && (o.pct ?? 0) >= 100;
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
              {stopped && (
                <p className="flex flex-wrap items-center gap-2 text-xs" data-testid={`account-usage-stopped-${o.slug}`}>
                  <StatusPill tone="warn" label="Stopped" title="Monthly budget reached" />
                  <span>
                    You can still read everything that's already generated. LLM spending resumes{" "}
                    {formatResetsAt(usage.data?.resets_at ?? "")}.
                  </span>
                </p>
              )}
              <div className="flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
                <span>
                  {formatNumber(o.calls)} {o.calls === 1 ? "call" : "calls"}
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

/** Your organizations: the name opens the org (its own host), with your role there. */
function MyOrganizations() {
  const orgs = useQuery({ queryKey: ACCOUNT_ORGS_KEY, queryFn: accountApi.orgs });
  return (
    <section
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card"
      data-testid="account-orgs"
    >
      <h2 className="text-sm font-semibold">Your organizations</h2>
      {orgs.isLoading && <Skeleton className="h-10" />}
      {orgs.isError && (
        <ErrorState error={orgs.error} title="Couldn't load your organizations" onRetry={() => void orgs.refetch()} />
      )}
      {orgs.data && orgs.data.length === 0 && (
        <p className="text-sm text-muted-foreground">
          You aren't in an organization yet.{" "}
          <Link to="/orgs/new" className="text-primary-text hover:underline">
            Create one
          </Link>
          .
        </p>
      )}
      {orgs.data && orgs.data.length > 0 && (
        <ul className="divide-y divide-border">
          {orgs.data.map((o) => (
            <li key={o.slug} className="row-wrap py-2.5" data-testid={`account-org-${o.slug}`}>
              <a
                href={safeHref(o.url)}
                className="min-w-0 flex-1 break-words font-medium text-primary-text hover:underline"
              >
                {o.name} <span className="font-mono text-xs font-normal text-muted-foreground">{o.slug}</span>
              </a>
              <Badge variant={o.role === "member" ? "outline" : "secondary"}>{orgRoleLabel(o.role)}</Badge>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * `/account`: display name and password. Lives on the base host, in its chrome
 * (which has the one Sign out, in the avatar menu). A GitHub account shows its
 * avatar and `@login` and has no password section.
 */
export function AccountPage() {
  const queryClient = useQueryClient();
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
      await queryClient.invalidateQueries({ queryKey: ["@account", "me"] });
      await queryClient.invalidateQueries({ queryKey: portalKey("state") });
    },
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
    <PageContainer className="flex flex-col gap-6 max-w-xl">
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

      <form noValidate onSubmit={onRename} className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5 shadow-card">
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
          {rename.isSuccess && !rename.isPending && name.trim() === account.data?.display_name && (
            <span role="status" className="ml-3 text-xs text-success" data-testid="name-saved">
              Name saved
            </span>
          )}
        </div>
        {rename.isError && <ErrorState error={rename.error} size="inline" context="auth" />}
      </form>

      {account.data?.has_password && (
        <form
          noValidate
          onSubmit={onPassword}
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
      )}

      <MyOrganizations />

      <MyUsage />

      <MyConnectedPortals />
    </PageContainer>
  );
}
