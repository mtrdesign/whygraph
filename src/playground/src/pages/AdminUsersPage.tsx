import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { adminApi, type AdminUser } from "../api";
import { AdminHeading } from "../components/admin/AdminHeading";
import { userMatches } from "../components/admin/AdminCommandPalette";
import { AdminSearchBox } from "../components/admin/AdminSearchBox";
import { ClearFilters } from "../components/portal/ClearFilters";
import { StatusFilter, userLabel } from "../components/admin/adminParts";
import { PageContainer } from "../components/layout/PageContainer";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { QueryState } from "../components/state/QueryState";
import { TableSkeleton } from "../components/state/Skeletons";
import { Badge } from "../components/ui/badge";
import { formatDate, timeAgo } from "../lib/format";
import { plural } from "../lib/plural";
import type { AdminUserStatus, AdminUsersSearch } from "../lib/routeSearch";

const STATUS_OPTIONS: { value: AdminUserStatus; label: string }[] = [
  { value: "admins", label: "Admins" },
  { value: "disabled", label: "Disabled" },
  { value: "deleted", label: "Deleted" },
  { value: "password", label: "Password accounts" },
];

/** Whether a row belongs to the "Show" option. "All" (no option) holds every row that is not deleted. */
function inStatus(u: AdminUser, status: AdminUserStatus | undefined): boolean {
  if (status === "deleted") return !!u.deleted_at;
  if (u.deleted_at) return false;
  if (status === "admins") return u.is_instance_admin;
  if (status === "disabled") return u.disabled;
  if (status === "password") return u.has_password;
  return true;
}

/** A password account that is not an administrator cannot sign in anywhere (D28). */
const cannotSignIn = (u: AdminUser) => u.has_password && !u.is_instance_admin;

function UserBadges({ user }: { user: AdminUser }) {
  return (
    <span className="flex flex-wrap gap-1">
      {user.deleted_at && <Badge variant="destructive">Deleted</Badge>}
      {user.is_instance_admin && <Badge variant="secondary">admin</Badge>}
      {user.disabled && <Badge variant="destructive">disabled</Badge>}
      {!user.deleted_at && cannotSignIn(user) && <Badge variant="outline">cannot sign in</Badge>}
      {user.has_password && <Badge variant="outline">password</Badge>}
      {!user.deleted_at && !user.is_instance_admin && !user.disabled && !user.has_password && (
        <span className="text-muted-foreground">-</span>
      )}
    </span>
  );
}

/** `/admin/users`: every account, filtered by "Show" and a search; each row opens its detail page. */
export function AdminUsersPage() {
  const { q, status } = useSearch({ strict: false }) as AdminUsersSearch;
  const navigate = useNavigate();
  const users = useQuery({ queryKey: ["@admin", "users"], queryFn: adminApi.users });
  const term = (q ?? "").trim().toLowerCase();
  const go = (next: AdminUsersSearch) => void navigate({ to: "/admin/users", search: next, replace: true });

  const columns: Column<AdminUser>[] = [
    {
      key: "who",
      header: "User",
      primary: true,
      cell: (u) => (
        <Link
          to="/admin/users/$uid"
          params={{ uid: u.uid }}
          className="flex min-w-0 flex-col leading-tight hover:underline"
        >
          <span className="truncate font-medium">{u.display_name}</span>
          <span className="truncate font-mono text-xs font-normal text-muted-foreground">{userLabel(u)}</span>
        </Link>
      ),
    },
    { key: "badges", header: "Badges", cell: (u) => <UserBadges user={u} /> },
    { key: "orgs", header: "Organizations", cell: (u) => plural(u.org_count, "org") },
    {
      key: "owns",
      header: "Owns",
      cell: (u) => (u.owner_of > 0 ? plural(u.owner_of, "org") : ""),
    },
    {
      key: "seen",
      header: "Last session seen",
      cell: (u) => <span className="text-muted-foreground">{timeAgo(u.session_last_seen_at) ?? "-"}</span>,
    },
    { key: "created", header: "Created", hideBelow: "md", cell: (u) => formatDate(u.created_at) },
  ];

  return (
    <PageContainer width="wide" className="flex flex-col gap-4">
      <AdminHeading description="Keep at least two administrators: there is no recovery path for a locked-out one.">
        Users
      </AdminHeading>
      <div className="flex flex-wrap items-end gap-3">
        <AdminSearchBox q={q} onChange={(next) => go({ status, q: next })} />
        <StatusFilter value={status} onChange={(next) => go({ status: next, q })} options={STATUS_OPTIONS} />
        {(q || status) && <ClearFilters onClear={() => go({})} />}
      </div>
      <QueryState query={users} loading={<TableSkeleton />} errorTitle="Couldn't load the users">
        {(all) => (
          <ResponsiveTable
            columns={columns}
            rows={all.filter((u) => inStatus(u, status) && (term === "" || userMatches(u, term)))}
            rowKey={(u) => u.uid}
            rowTestId={(u) => `admin-user-${u.uid}`}
            onRowClick={(u) => void navigate({ to: "/admin/users/$uid", params: { uid: u.uid } })}
            empty={<p className="text-sm text-muted-foreground">No user matches.</p>}
          />
        )}
      </QueryState>
    </PageContainer>
  );
}
