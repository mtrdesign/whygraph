import { useQuery } from "@tanstack/react-query";
import { useSearch } from "@tanstack/react-router";
import { adminApi, type AdminUser } from "../api";
import { AdminHeading } from "../components/admin/AdminHeading";
import { userMatches } from "../components/admin/AdminCommandPalette";
import { AdminSearchBox } from "../components/admin/AdminSearchBox";
import { UserActions } from "../components/admin/UserActions";
import { PageContainer } from "../components/layout/PageContainer";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { QueryState } from "../components/state/QueryState";
import { TableSkeleton } from "../components/state/Skeletons";
import { Badge } from "../components/ui/badge";
import { plural } from "../lib/plural";

/** `/admin/users`: every account with today's actions (the full module is Phase 2). */
export function AdminUsersPage() {
  const { q } = useSearch({ strict: false }) as { q?: string };
  const me = useQuery({ queryKey: ["@admin", "me"], queryFn: adminApi.me }).data?.uid;
  const users = useQuery({ queryKey: ["@admin", "users"], queryFn: adminApi.users });
  const term = (q ?? "").trim().toLowerCase();

  const columns: Column<AdminUser>[] = [
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
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      cell: (u) => <UserActions user={u} isMe={u.uid === me} />,
    },
  ];

  return (
    <PageContainer width="wide" className="flex flex-col gap-4">
      <AdminHeading description="Keep at least two administrators: there is no recovery path for a locked-out one.">
        Users
      </AdminHeading>
      <AdminSearchBox to="/admin/users" q={q} />
      <QueryState query={users} loading={<TableSkeleton />} errorTitle="Couldn't load the users">
        {(all) => (
          <ResponsiveTable
            columns={columns}
            rows={all.filter((u) => term === "" || userMatches(u, term))}
            rowKey={(u) => u.uid}
            rowTestId={(u) => `admin-user-${u.uid}`}
            empty={<p className="text-sm text-muted-foreground">No user matches that search.</p>}
          />
        )}
      </QueryState>
    </PageContainer>
  );
}
