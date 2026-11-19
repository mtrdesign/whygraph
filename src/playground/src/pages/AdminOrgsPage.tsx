import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { adminApi, type AdminOrg } from "../api";
import { AdminHeading } from "../components/admin/AdminHeading";
import { orgMatches } from "../components/admin/AdminCommandPalette";
import { AdminSearchBox } from "../components/admin/AdminSearchBox";
import { CreateOrgDialog } from "../components/admin/OrgDialogs";
import { StatusFilter } from "../components/admin/adminParts";
import { PageContainer } from "../components/layout/PageContainer";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { EmptyState } from "../components/state/EmptyState";
import { QueryState } from "../components/state/QueryState";
import { TableSkeleton } from "../components/state/Skeletons";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { formatBytes, formatDate, formatUsd } from "../lib/format";
import { plural } from "../lib/plural";
import type { AdminOrgStatus, AdminOrgsSearch } from "../lib/routeSearch";

const STATUS_OPTIONS: { value: AdminOrgStatus; label: string }[] = [
  { value: "suspended", label: "Suspended" },
  { value: "deleted", label: "Deleted" },
];

/** "All" holds every organization that is not deleted; a suspended one is in All with its badge. */
function inStatus(o: AdminOrg, status: AdminOrgStatus | undefined): boolean {
  if (status === "deleted") return !!o.deleted_at;
  if (o.deleted_at) return false;
  return status === "suspended" ? !!o.suspended : true;
}

const columns: Column<AdminOrg>[] = [
  {
    key: "org",
    header: "Organization",
    primary: true,
    cell: (o) => (
      <Link to="/admin/orgs/$slug" params={{ slug: o.slug }} className="flex min-w-0 flex-col leading-tight hover:underline">
        <span className="flex items-center gap-1.5">
          <span className="truncate font-medium">{o.name}</span>
          {o.deleted_at && <Badge variant="destructive">Deleted</Badge>}
          {o.suspended && !o.deleted_at && <Badge variant="outline">Suspended</Badge>}
        </span>
        <span className="truncate font-mono text-xs font-normal text-muted-foreground">{o.slug}</span>
      </Link>
    ),
  },
  { key: "members", header: "Members", align: "right", cell: (o) => plural(o.member_count, "member") },
  { key: "projects", header: "Projects", align: "right", cell: (o) => plural(o.project_count, "project") },
  {
    key: "owners",
    header: "Owners",
    hideBelow: "md",
    cell: (o) => (o.owners.length > 0 ? o.owners.map((l) => `@${l}`).join(", ") : <span className="text-muted-foreground">-</span>),
  },
  { key: "spend", header: "Spend this month", align: "right", cell: (o) => formatUsd(o.spend_mtd) },
  {
    key: "storage",
    header: "Storage",
    align: "right",
    cell: (o) => (o.storage_bytes === null ? <span className="text-muted-foreground">-</span> : formatBytes(o.storage_bytes)),
  },
  {
    key: "activity",
    header: "Last activity",
    hideBelow: "md",
    cell: (o) => (o.last_activity_day ? formatDate(o.last_activity_day) : <span className="text-muted-foreground">-</span>),
  },
  { key: "created", header: "Created", hideBelow: "md", cell: (o) => formatDate(o.created_at) },
];

/** `/admin/orgs`: every organization, filtered by "Show" and a search; Create organization is here. */
export function AdminOrgsPage() {
  const { q, status } = useSearch({ strict: false }) as AdminOrgsSearch;
  const navigate = useNavigate();
  const orgs = useQuery({ queryKey: ["@admin", "orgs"], queryFn: adminApi.orgs });
  const [creating, setCreating] = useState(false);
  const term = (q ?? "").trim().toLowerCase();
  const go = (next: AdminOrgsSearch) => void navigate({ to: "/admin/orgs", search: next, replace: true });
  return (
    <PageContainer width="wide" className="flex flex-col gap-4">
      <div className="row-wrap">
        <div className="flex-1">
          <AdminHeading>Organizations</AdminHeading>
        </div>
        <Button onClick={() => setCreating(true)}>Create organization</Button>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <AdminSearchBox q={q} onChange={(next) => go({ status, q: next })} />
        <StatusFilter value={status} onChange={(next) => go({ status: next, q })} options={STATUS_OPTIONS} />
      </div>
      <QueryState query={orgs} loading={<TableSkeleton />} errorTitle="Couldn't load the organizations">
        {(all) => (
          <ResponsiveTable
            columns={columns}
            rows={all.filter((o) => inStatus(o, status) && (term === "" || orgMatches(o, term)))}
            rowKey={(o) => o.slug}
            rowTestId={(o) => `admin-org-${o.slug}`}
            onRowClick={(o) => void navigate({ to: "/admin/orgs/$slug", params: { slug: o.slug } })}
            empty={
              all.length === 0 ? (
                <EmptyState
                  title="No organizations yet"
                  description="People create them after signing in with GitHub, or use Create organization."
                />
              ) : (
                <p className="text-sm text-muted-foreground">No organization matches.</p>
              )
            }
          />
        )}
      </QueryState>
      <CreateOrgDialog open={creating} onOpenChange={setCreating} />
    </PageContainer>
  );
}
