import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { adminApi, type AdminUser } from "../api";
import { CopyButton } from "../components/portal/CopyButton";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";

const USERS = ["@admin", "users"] as const;

/** One user's row: the admin toggle and a one-time reset link (shown once, to copy). */
function UserRow({ user }: { user: AdminUser }) {
  const queryClient = useQueryClient();
  const [link, setLink] = useState<string | null>(null);
  const setAdmin = useMutation({
    mutationFn: (value: boolean) => adminApi.setAdmin(user.uid, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: USERS }),
    onError: (err) => toast.error(err.message),
  });
  const reset = useMutation({
    mutationFn: () => adminApi.resetLink(user.uid),
    onSuccess: ({ url }) => setLink(url),
    onError: (err) => toast.error(err.message),
  });
  return (
    <li className="flex flex-col gap-2 py-3" data-testid={`admin-user-${user.email}`}>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex min-w-0 flex-1 flex-col leading-tight">
          <span className="truncate font-medium">
            {user.display_name}
          </span>
          <span className="truncate font-mono text-xs text-muted-foreground">{user.email}</span>
        </div>
        <span className="text-xs text-muted-foreground">
          {user.org_count} org{user.org_count === 1 ? "" : "s"}
        </span>
        {user.is_instance_admin && <Badge variant="secondary">admin</Badge>}
        <Button
          size="sm"
          variant="outline"
          disabled={setAdmin.isPending}
          onClick={() => setAdmin.mutate(!user.is_instance_admin)}
        >
          {user.is_instance_admin ? "Remove admin" : "Make admin"}
        </Button>
        <Button size="sm" variant="outline" disabled={reset.isPending} onClick={() => reset.mutate()}>
          Copy reset link
        </Button>
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
    </li>
  );
}

/** `/admin` on the base host: users, organizations and the base-URL self-check. */
export function AdminPage() {
  const settings = useQuery({ queryKey: ["@admin", "settings"], queryFn: adminApi.settings });
  const users = useQuery({ queryKey: USERS, queryFn: adminApi.users });
  const orgs = useQuery({ queryKey: ["@admin", "orgs"], queryFn: adminApi.orgs });
  const warnings = settings.data?.base_check ?? [];

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6 sm:p-8">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight">Administration</h1>
        {settings.data && (
          <p className="font-mono text-[13px] text-muted-foreground">{settings.data.base_url}</p>
        )}
      </div>

      {settings.data && warnings.length > 0 && (
        <Alert variant="destructive" data-testid="base-check">
          <AlertTitle>The base URL check found problems</AlertTitle>
          <AlertDescription>
            <ul className="list-disc pl-4">
              {warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}

      <section className="flex flex-col gap-1 rounded-xl border border-border bg-card p-5">
        <h2 className="text-sm font-semibold">Users</h2>
        <p className="text-xs text-muted-foreground">
          Keep at least two administrators: there is no recovery path for a locked-out one.
        </p>
        {users.isLoading && <Skeleton className="mt-2 h-16" />}
        {users.isError && <p className="text-sm text-destructive">Failed to load: {users.error.message}</p>}
        {users.data && (
          <ul className="divide-y divide-border">
            {users.data.map((u) => (
              <UserRow key={u.uid} user={u} />
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-1 rounded-xl border border-border bg-card p-5">
        <h2 className="text-sm font-semibold">Organizations</h2>
        {orgs.isLoading && <Skeleton className="mt-2 h-16" />}
        {orgs.isError && <p className="text-sm text-destructive">Failed to load: {orgs.error.message}</p>}
        {orgs.data && orgs.data.length === 0 && (
          <p className="text-sm text-muted-foreground">No organizations yet.</p>
        )}
        {orgs.data && orgs.data.length > 0 && (
          <ul className="divide-y divide-border">
            {orgs.data.map((o) => (
              <li key={o.slug} className="flex items-center gap-3 py-2.5">
                <a href={o.url} className="min-w-0 flex-1 truncate font-medium text-primary-text hover:underline">
                  {o.name} <span className="font-mono text-xs text-muted-foreground">{o.slug}</span>
                </a>
                <span className="text-xs text-muted-foreground">
                  {o.member_count} member{o.member_count === 1 ? "" : "s"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
