import { PageContainer } from "../components/layout/PageContainer";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { adminApi, type AdminOrg, type AdminUser } from "../api";
import { ConfirmDialog } from "../components/portal/ConfirmDialog";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { ErrorState } from "../components/state/ErrorState";
import { AuditTable } from "../components/portal/AuditTable";
import { CopyButton } from "../components/portal/CopyButton";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";
import { authMessage } from "../lib/authErrors";
import { usePortalState } from "../lib/identity";
import { plural } from "../lib/plural";

const USERS = ["@admin", "users"] as const;

/**
 * One user's actions: Make / Remove admin (secondary), Disable (a destructive
 * outline, after a confirmation) / Enable and, for a password account, a one-time
 * reset link (shown once, to copy).
 */
function UserActions({ user, isMe }: { user: AdminUser; isMe: boolean }) {
  const queryClient = useQueryClient();
  const [link, setLink] = useState<string | null>(null);
  const [disabling, setDisabling] = useState(false);
  const setAdmin = useMutation({
    mutationFn: (value: boolean) => adminApi.setAdmin(user.uid, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: USERS }),
    onError: (err) => toast.error(authMessage(err)),
  });
  const setDisabled = useMutation({
    mutationFn: (value: boolean) => adminApi.setDisabled(user.uid, value),
    onSuccess: async () => {
      setDisabling(false);
      await queryClient.invalidateQueries({ queryKey: USERS });
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

/** `/admin` on the base host: users, organizations and the base-URL self-check. */
export function AdminPage() {
  const me = usePortalState().data?.user?.uid;
  const settings = useQuery({ queryKey: ["@admin", "settings"], queryFn: adminApi.settings });
  const users = useQuery({ queryKey: USERS, queryFn: adminApi.users });
  const orgs = useQuery({ queryKey: ["@admin", "orgs"], queryFn: adminApi.orgs });
  const checks = settings.data?.base_check ?? null;

  const userColumns: Column<AdminUser>[] = [
    {
      key: "who",
      header: "User",
      primary: true,
      cell: (u) => (
        <span className="flex min-w-0 flex-col leading-tight">
          <span className="truncate font-medium">{u.display_name}</span>
          <span className="truncate font-mono text-xs font-normal text-muted-foreground">
            {u.github_login ? `@${u.github_login}` : u.email}
          </span>
        </span>
      ),
    },
    { key: "orgs", header: "Organizations", cell: (u) => plural(u.org_count, "org") },
    {
      key: "status",
      header: "Status",
      cell: (u) => (
        <span className="flex flex-wrap gap-1">
          {u.is_instance_admin && <Badge variant="secondary">admin</Badge>}
          {u.disabled && <Badge variant="destructive">disabled</Badge>}
          {!u.is_instance_admin && !u.disabled && <span className="text-muted-foreground">-</span>}
        </span>
      ),
    },
    { key: "actions", header: <span className="sr-only">Actions</span>, cell: (u) => <UserActions user={u} isMe={u.uid === me} /> },
  ];
  const orgColumns: Column<AdminOrg>[] = [
    {
      key: "org",
      header: "Organization",
      primary: true,
      cell: (o) => (
        <a href={o.url} className="text-primary-text hover:underline">
          {o.name} <span className="font-mono text-xs text-muted-foreground">{o.slug}</span>
        </a>
      ),
    },
    { key: "members", header: "Members", align: "right", cell: (o) => plural(o.member_count, "member") },
  ];

  return (
    <PageContainer width="narrow" className="flex flex-col gap-6">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight">Administration</h1>
        {settings.data && (
          <p className="break-all font-mono text-[13px] text-muted-foreground">{settings.data.base_url}</p>
        )}
      </div>

      <section
        className="flex flex-col gap-2 rounded-xl border border-border bg-card p-5 shadow-card"
        data-testid="settings-check"
      >
        <h2 className="text-sm font-semibold">Settings check</h2>
        {settings.isLoading && <Skeleton className="h-10" />}
        {settings.isError && (
          <ErrorState error={settings.error} title="Couldn't load the settings check" onRetry={() => void settings.refetch()} />
        )}
        {settings.data && checks === null && (
          <p className="text-sm text-muted-foreground">The base URL check has not run yet.</p>
        )}
        {settings.data && checks !== null && checks.length === 0 && (
          <Alert variant="success" data-testid="base-check-ok">
            <AlertDescription>The base URL check found no problems.</AlertDescription>
          </Alert>
        )}
        {settings.data && checks !== null && checks.length > 0 && (
          <Alert variant="destructive" data-testid="base-check">
            <AlertTitle>The base URL check found problems</AlertTitle>
            <AlertDescription>
              <ul className="list-disc pl-4">
                {checks.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}
      </section>

      <section className="flex flex-col gap-2 rounded-xl border border-border bg-card p-5 shadow-card">
        <h2 className="text-sm font-semibold">Users</h2>
        <p className="text-xs text-muted-foreground">
          Keep at least two administrators: there is no recovery path for a locked-out one.
        </p>
        {users.isLoading && <Skeleton className="mt-2 h-16" />}
        {users.isError && (
          <ErrorState error={users.error} title="Couldn't load the users" onRetry={() => void users.refetch()} />
        )}
        {users.data && (
          <ResponsiveTable
            columns={userColumns}
            rows={users.data}
            rowKey={(u) => u.uid}
            rowTestId={(u) => `admin-user-${u.uid}`}
          />
        )}
      </section>

      <section className="flex flex-col gap-2 rounded-xl border border-border bg-card p-5 shadow-card">
        <h2 className="text-sm font-semibold">Organizations</h2>
        {orgs.isLoading && <Skeleton className="mt-2 h-16" />}
        {orgs.isError && (
          <ErrorState error={orgs.error} title="Couldn't load the organizations" onRetry={() => void orgs.refetch()} />
        )}
        {orgs.data && orgs.data.length === 0 && (
          <p className="text-sm text-muted-foreground">No organizations yet. People create them from the Organizations page.</p>
        )}
        {orgs.data && orgs.data.length > 0 && (
          <ResponsiveTable columns={orgColumns} rows={orgs.data} rowKey={(o) => o.slug} />
        )}
      </section>
      <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card" data-testid="admin-audit">
        <div>
          <h2 className="text-sm font-semibold">Security events</h2>
          <p className="text-xs text-muted-foreground">Sign-ins, instance administration and deleted organizations' events.</p>
        </div>
        <AuditTable scope="admin" />
      </section>
    </PageContainer>
  );
}
