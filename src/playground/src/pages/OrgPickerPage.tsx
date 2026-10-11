import { useEffect } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { accountApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { ACCOUNT_ORGS_KEY, orgRoleText } from "../components/shell/OrgSwitcher";
import { ErrorState } from "../components/state/ErrorState";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";
import { usePortalState } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";

/**
 * `<base>/orgs`: the organizations you belong to. Exactly one goes straight in,
 * unless you arrived with a `next` (then the list is the answer, so a failed
 * hand-off cannot loop) or with `?stay=1` (you asked for the list: the org
 * switcher's "All organizations"). None shows how to start or join one, never a
 * redirect (BUG-19).
 */
export function OrgPickerPage() {
  const { next, stay } = useSearch({ strict: false }) as { next?: string; stay?: boolean };
  const login = usePortalState().data?.user?.github_login;
  const orgs = useQuery({ queryKey: ACCOUNT_ORGS_KEY, queryFn: accountApi.orgs, staleTime: 0 });
  const list = orgs.data ?? [];
  const handOff = orgs.isSuccess && list.length === 1 && !next && !stay;

  useEffect(() => {
    if (handOff) void hardNavigate(list[0].url);
  }, [handOff, list]);

  if (orgs.isSuccess && list.length === 0) {
    return (
      <AuthLayout
        title="You're not in an organization yet"
        description="Create one for your team, or ask to join an existing one."
      >
        <Button render={<Link to="/orgs/new" />}>Create organization</Button>
        <p className="text-sm text-muted-foreground" data-testid="join-hint">
          Joining a team? Ask an owner to add{" "}
          {login ? (
            <>
              your GitHub username <span className="font-mono text-foreground">@{login}</span>
            </>
          ) : (
            "you"
          )}
          .
        </p>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Your organizations"
      description="Pick an organization to open."
      footer={
        <Link to="/orgs/new" className="text-primary-text hover:underline">
          Create organization
        </Link>
      }
    >
      {(orgs.isLoading || handOff) && <Skeleton className="h-16 rounded-lg" />}
      {orgs.isError && (
        <ErrorState error={orgs.error} title="Couldn't load your organizations" onRetry={() => void orgs.refetch()} size="section" />
      )}
      {orgs.isSuccess && !handOff && (
        <ul className="flex flex-col gap-2" data-testid="org-list">
          {list.map((o) => (
            <li key={o.slug}>
              <a
                href={o.url}
                className="flex items-center gap-3 rounded-lg border border-border px-3 py-2 outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="flex size-7 shrink-0 items-center justify-center rounded bg-muted text-sm font-semibold">
                  {o.name.charAt(0).toUpperCase()}
                </span>
                <span className="flex min-w-0 flex-1 flex-col leading-tight">
                  <span className="truncate font-medium">{o.name}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">{o.slug}</span>
                </span>
                {o.new && (
                  <span className="flex items-center gap-1 text-xs text-primary-text" data-testid="org-new">
                    <span aria-hidden className="size-1.5 rounded-full bg-primary-text" />
                    New
                  </span>
                )}
                <Badge variant="outline">{orgRoleText(o.role)}</Badge>
              </a>
            </li>
          ))}
        </ul>
      )}
    </AuthLayout>
  );
}
