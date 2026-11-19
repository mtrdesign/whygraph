import { useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { adminApi, type AdminSession, type AdminToken, type AdminUserDetail } from "../api";
import { UserAvatar } from "../components/auth/UserAvatar";
import { AdminHeading } from "../components/admin/AdminHeading";
import { ADMIN, AdminNotFound, DeletedAlert, hasStatus, userLabel } from "../components/admin/adminParts";
import { UserActions } from "../components/admin/UserActions";
import { PageContainer } from "../components/layout/PageContainer";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { ConfirmDialog } from "../components/portal/ConfirmDialog";
import { ErrorState } from "../components/state/ErrorState";
import { FormSkeleton } from "../components/state/Skeletons";
import { Alert, AlertDescription } from "../components/ui/alert";
import { AuditRows } from "../components/portal/AuditTable";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { errorMessage } from "../lib/apiErrors";
import { formatDate, formatDateTime, formatUsd, timeAgo } from "../lib/format";

const CARD = "flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card";

/** A user's sessions with a Revoke on each, and "Sign out everywhere" (every kind). */
function Sessions({ user, writable }: { user: AdminUserDetail; writable: boolean }) {
  const queryClient = useQueryClient();
  const [one, setOne] = useState<AdminSession | null>(null);
  const [all, setAll] = useState(false);
  const done = () => queryClient.invalidateQueries({ queryKey: ADMIN });
  const revokeOne = useMutation({
    mutationFn: (id: string) => adminApi.revokeSession(user.uid, id),
    onSuccess: async () => {
      setOne(null);
      await done();
    },
  });
  const revokeAll = useMutation({
    mutationFn: () => adminApi.revokeSessions(user.uid),
    onSuccess: async () => {
      setAll(false);
      await done();
    },
  });
  const columns: Column<AdminSession>[] = [
    {
      key: "kind",
      header: "Session",
      primary: true,
      cell: (s) => (
        <span className="flex flex-col leading-tight">
          <span className="font-medium">{s.kind === "admin" ? "Administration" : "Everyday"}</span>
          <span className="truncate text-xs text-muted-foreground">{s.user_agent ?? "Unknown browser"}</span>
        </span>
      ),
    },
    { key: "started", header: "Started", cell: (s) => formatDateTime(s.created_at) },
    { key: "seen", header: "Last seen", cell: (s) => timeAgo(s.last_seen_at) ?? "-" },
    ...(writable
      ? [
          {
            key: "revoke",
            header: <span className="sr-only">Actions</span>,
            cell: (s: AdminSession) => (
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  revokeOne.reset();
                  setOne(s);
                }}
              >
                Revoke
              </Button>
            ),
          },
        ]
      : []),
  ];
  return (
    <section className={CARD} data-testid="user-sessions">
      <div className="row-wrap">
        <h2 className="flex-1 text-sm font-semibold">Sessions</h2>
        {writable && user.sessions.length > 0 && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              revokeAll.reset();
              setAll(true);
            }}
          >
            Sign out everywhere
          </Button>
        )}
      </div>
      <ResponsiveTable
        columns={columns}
        rows={user.sessions}
        rowKey={(s) => s.id}
        empty={<p className="text-sm text-muted-foreground">No open sessions.</p>}
      />
      <ConfirmDialog
        open={one !== null}
        onOpenChange={(o) => !o && setOne(null)}
        title="End this session?"
        description="The browser using it is signed out at once."
        confirmLabel="End session"
        pending={revokeOne.isPending}
        error={revokeOne.isError ? errorMessage(revokeOne.error) : null}
        onConfirm={() => one && revokeOne.mutate(one.id)}
      />
      <ConfirmDialog
        open={all}
        onOpenChange={setAll}
        title={`Sign ${user.display_name} out everywhere?`}
        description={`Every session of ${user.display_name} ends at once, admin sessions included. Their connection tokens keep working.`}
        confirmLabel="Sign out everywhere"
        pending={revokeAll.isPending}
        error={revokeAll.isError ? errorMessage(revokeAll.error) : null}
        onConfirm={() => revokeAll.mutate()}
      />
    </section>
  );
}

/** A user's connection tokens (live and revoked), with Revoke on a live one. */
function Tokens({ user, writable }: { user: AdminUserDetail; writable: boolean }) {
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<AdminToken | null>(null);
  const revoke = useMutation({
    mutationFn: (t: AdminToken) => adminApi.revokeToken(user.uid, t.uid),
    onSuccess: async () => {
      setTarget(null);
      await queryClient.invalidateQueries({ queryKey: ADMIN });
    },
  });
  const columns: Column<AdminToken>[] = [
    {
      key: "client",
      header: "Connection",
      primary: true,
      cell: (t) => <span className="font-medium">{t.client_name}</span>,
    },
    {
      key: "project",
      header: "Project",
      cell: (t) => (
        <span className="flex flex-col leading-tight">
          <span>{t.project_name ?? t.project}</span>
          {t.org && <span className="font-mono text-xs text-muted-foreground">{t.org}</span>}
        </span>
      ),
    },
    { key: "used", header: "Last used", cell: (t) => timeAgo(t.last_used_at) ?? "Never" },
    {
      key: "state",
      header: "State",
      cell: (t) =>
        t.revoked_at ? (
          <Badge variant="outline">Revoked</Badge>
        ) : writable ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              revoke.reset();
              setTarget(t);
            }}
          >
            Revoke
          </Button>
        ) : null,
    },
  ];
  return (
    <section className={CARD} data-testid="user-tokens">
      <h2 className="text-sm font-semibold">Connections</h2>
      <ResponsiveTable
        columns={columns}
        rows={user.tokens}
        rowKey={(t) => t.uid}
        empty={<p className="text-sm text-muted-foreground">No connected machines.</p>}
      />
      <ConfirmDialog
        open={target !== null}
        onOpenChange={(o) => !o && setTarget(null)}
        title={`Revoke the connection "${target?.client_name ?? ""}"?`}
        description={`Agents on that machine lose access to ${target?.project_name ?? target?.project ?? "the project"} at once. Its owner can connect it again.`}
        confirmLabel="Revoke"
        pending={revoke.isPending}
        error={revoke.isError ? errorMessage(revoke.error) : null}
        onConfirm={() => target && revoke.mutate(target)}
      />
    </section>
  );
}

function Detail({ user, isMe }: { user: AdminUserDetail; isMe: boolean }) {
  const gone = !!user.deleted_at || user.erased;
  const writable = !gone;
  return (
    <PageContainer width="default" className="flex flex-col gap-5" data-testid="admin-user">
      <div className="flex items-center gap-3">
        <UserAvatar name={user.display_name} url={null} className="size-10 text-sm" />
        <div className="min-w-0">
          <AdminHeading>{user.display_name}</AdminHeading>
          <p className="truncate font-mono text-[13px] text-muted-foreground">{userLabel(user)}</p>
        </div>
      </div>
      {user.erased ? (
        <Alert variant="warning" data-testid="erased-alert">
          <AlertDescription>This account was erased. Nothing about it can be changed any more.</AlertDescription>
        </Alert>
      ) : (
        user.deleted_at && <DeletedAlert deletedAt={user.deleted_at} deletedBy={user.deleted_by} />
      )}
      <UserActions user={user} isMe={isMe} />

      <section className={CARD} data-testid="user-profile">
        <h2 className="text-sm font-semibold">Profile</h2>
        <div className="flex flex-wrap gap-1">
          {user.is_instance_admin && <Badge variant="secondary">admin</Badge>}
          {user.disabled && <Badge variant="destructive">disabled</Badge>}
          {user.has_password && !user.is_instance_admin && <Badge variant="outline">cannot sign in</Badge>}
          {user.has_password && <Badge variant="outline">password</Badge>}
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Created</dt>
          <dd>{formatDate(user.created_at)}</dd>
          <dt className="text-muted-foreground">Last session seen</dt>
          <dd>{timeAgo(user.session_last_seen_at) ?? "-"}</dd>
        </dl>
      </section>

      <section className={CARD} data-testid="user-memberships">
        <h2 className="text-sm font-semibold">Memberships</h2>
        <ResponsiveTable
          columns={[
            {
              key: "org",
              header: "Organization",
              primary: true,
              cell: (m) => (
                <Link to="/admin/orgs/$slug" params={{ slug: m.org }} className="font-medium hover:underline">
                  {m.name}
                </Link>
              ),
            },
            { key: "slug", header: "Slug", cell: (m) => <span className="font-mono text-xs">{m.org}</span> },
            { key: "role", header: "Role", cell: (m) => <span className="capitalize">{m.role}</span> },
          ]}
          rows={user.memberships}
          rowKey={(m) => m.org}
          empty={<p className="text-sm text-muted-foreground">Not a member of any organization.</p>}
        />
      </section>

      <Sessions user={user} writable={writable} />
      <Tokens user={user} writable={writable} />

      <section className={CARD} data-testid="user-spend">
        <h2 className="text-sm font-semibold">Spend this month</h2>
        {user.spend.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing spent this month.</p>
        ) : (
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            {user.spend.map((s) => (
              <div key={s.org} className="contents">
                <dt className="font-mono text-muted-foreground">{s.org}</dt>
                <dd className="tabular-nums">{formatUsd(s.spent_usd)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <section className="flex flex-col gap-3" data-testid="user-events">
        <div className="row-wrap">
          <h2 className="flex-1 text-sm font-semibold">Latest events</h2>
          {!user.erased && (
            <Link
              to="/admin/audit"
              search={{ actor: user.github_login ? `@${user.github_login}` : user.uid }}
              className="text-sm text-primary-text hover:underline"
            >
              All events by this user
            </Link>
          )}
        </div>
        {user.events.length === 0 ? (
          <p className="text-sm text-muted-foreground">No events yet.</p>
        ) : (
          <AuditRows rows={user.events} admin showOrg />
        )}
      </section>
    </PageContainer>
  );
}

/** `/admin/users/$uid`: one account, its memberships, sessions, connections, spend and latest events. */
export function AdminUserPage() {
  const { uid } = useParams({ strict: false }) as { uid: string };
  const me = useQuery({ queryKey: [...ADMIN, "me"], queryFn: adminApi.me });
  const user = useQuery({ queryKey: [...ADMIN, "user", uid], queryFn: () => adminApi.user(uid), retry: false });
  if (user.data === undefined) {
    if (hasStatus(user.error, 404)) return <AdminNotFound label="Back to users" to="/admin/users" />;
    if (user.isError) {
      return (
        <PageContainer width="narrow">
          <ErrorState error={user.error} title="Couldn't load the user" onRetry={() => void user.refetch()} />
        </PageContainer>
      );
    }
    return (
      <PageContainer width="default">
        <FormSkeleton />
      </PageContainer>
    );
  }
  return <Detail user={user.data} isMe={user.data.uid === me.data?.uid} />;
}
