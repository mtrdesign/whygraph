import { useEffect } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { accountApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Badge } from "../components/ui/badge";
import { Skeleton } from "../components/ui/skeleton";
import { hardNavigate } from "../lib/navigation";

/**
 * `<base>/orgs`: the organizations you belong to. No orgs goes straight to
 * creating one; exactly one goes straight in - unless you arrived with a `next`
 * (then the list is the answer, so a failed hand-off cannot loop).
 */
export function OrgPickerPage() {
  const { next } = useSearch({ strict: false }) as { next?: string };
  const navigate = useNavigate();
  const orgs = useQuery({ queryKey: ["@account", "orgs"], queryFn: accountApi.orgs, staleTime: 0 });
  const list = orgs.data ?? [];

  useEffect(() => {
    if (!orgs.isSuccess) return;
    if (list.length === 0) void navigate({ to: "/orgs/new", replace: true });
    else if (list.length === 1 && !next) void hardNavigate(list[0].url);
  }, [orgs.isSuccess, list, next, navigate]);

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
      {orgs.isLoading && <Skeleton className="h-16 rounded-lg" />}
      {orgs.isError && <p className="text-sm text-destructive">Failed to load: {orgs.error.message}</p>}
      {orgs.isSuccess && list.length > 1 && (
        <ul className="flex flex-col gap-2" data-testid="org-list">
          {list.map((o) => (
            <li key={o.slug}>
              <a
                href={o.url}
                className="flex items-center gap-3 rounded-lg border border-border px-3 py-2 hover:bg-accent"
              >
                <span className="flex size-7 shrink-0 items-center justify-center rounded bg-muted text-sm font-semibold">
                  {o.name.charAt(0).toUpperCase()}
                </span>
                <span className="flex min-w-0 flex-1 flex-col leading-tight">
                  <span className="truncate font-medium">{o.name}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">{o.slug}</span>
                </span>
                <Badge variant="outline">{o.role}</Badge>
              </a>
            </li>
          ))}
        </ul>
      )}
      {orgs.isSuccess && list.length === 1 && next && (
        <ul className="flex flex-col gap-2" data-testid="org-list">
          <li>
            <a href={list[0].url} className="font-medium text-primary-text hover:underline">
              {list[0].name}
            </a>
          </li>
        </ul>
      )}
    </AuthLayout>
  );
}
