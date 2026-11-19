import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { adminApi, type AdminProjectRow } from "../api";
import { AdminHeading } from "../components/admin/AdminHeading";
import { projectMatches } from "../components/admin/AdminCommandPalette";
import { AdminSearchBox } from "../components/admin/AdminSearchBox";
import { StatusFilter } from "../components/admin/adminParts";
import { projectState } from "../components/admin/ProjectActions";
import { PageContainer } from "../components/layout/PageContainer";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { nativeSelect } from "../components/portal/Field";
import { EmptyState } from "../components/state/EmptyState";
import { QueryState } from "../components/state/QueryState";
import { TableSkeleton } from "../components/state/Skeletons";
import { Badge } from "../components/ui/badge";
import { formatBytes, formatUsd, timeAgo } from "../lib/format";
import type { AdminProjectState, AdminProjectsSearch } from "../lib/routeSearch";

const STATE_OPTIONS: { value: AdminProjectState; label: string }[] = [
  { value: "ok", label: "OK" },
  { value: "failing", label: "Failing" },
  { value: "access_lost", label: "Access lost" },
  { value: "never_scanned", label: "Never scanned" },
  { value: "deleted", label: "Deleted" },
];

/** A list row as the state badge reads it. */
export function rowState(p: AdminProjectRow) {
  return projectState({
    deleted_at: p.deleted_at,
    access_lost_reason: p.access_lost_reason,
    last_run_status: p.last_run?.status ?? null,
    last_scan_at: p.last_scan_at,
  });
}

/** "All" holds every project that is not deleted; Deleted only the deleted ones. */
function inState(p: AdminProjectRow, state: AdminProjectState | undefined): boolean {
  const key = rowState(p).key;
  if (state === "deleted") return key === "deleted";
  if (key === "deleted") return false;
  return state ? key === state : true;
}

const columns: Column<AdminProjectRow>[] = [
  {
    key: "project",
    header: "Project",
    primary: true,
    cell: (p) => (
      <Link
        to="/admin/projects/$org/$slug"
        params={{ org: p.org, slug: p.slug }}
        className="flex min-w-0 flex-col leading-tight hover:underline"
      >
        <span className="truncate font-medium">{p.name}</span>
        <span className="truncate font-mono text-xs font-normal text-muted-foreground">
          {p.org}/{p.slug}
        </span>
      </Link>
    ),
  },
  {
    key: "state",
    header: "State",
    cell: (p) => {
      const s = rowState(p);
      return <Badge variant={s.variant}>{s.label}</Badge>;
    },
  },
  { key: "scan", header: "Last scan", cell: (p) => timeAgo(p.last_scan_at) ?? "-" },
  {
    key: "storage",
    header: "Storage",
    align: "right",
    cell: (p) => (p.storage_bytes === null ? <span className="text-muted-foreground">-</span> : formatBytes(p.storage_bytes)),
  },
  { key: "spend", header: "Spend this month", align: "right", cell: (p) => formatUsd(p.spend_mtd) },
];

/** `/admin/projects`: every project of every organization, filtered by organization, state and a search. */
export function AdminProjectsPage() {
  const { q, org, state } = useSearch({ strict: false }) as AdminProjectsSearch;
  const navigate = useNavigate();
  const projects = useQuery({ queryKey: ["@admin", "projects"], queryFn: adminApi.projects });
  const term = (q ?? "").trim().toLowerCase();
  const go = (next: AdminProjectsSearch) => void navigate({ to: "/admin/projects", search: next, replace: true });
  const orgSlugs = [...new Set((projects.data?.projects ?? []).map((p) => p.org))];
  return (
    <PageContainer width="wide" className="flex flex-col gap-4">
      <AdminHeading>Projects</AdminHeading>
      <div className="flex flex-wrap items-end gap-3">
        <AdminSearchBox q={q} onChange={(next) => go({ org, state, q: next })} />
        <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium sm:w-48">
          Organization
          <select
            className={nativeSelect("font-normal")}
            value={org ?? ""}
            onChange={(e) => go({ org: e.target.value || undefined, state, q })}
            data-testid="org-filter"
          >
            <option value="">All</option>
            {org && !orgSlugs.includes(org) && <option value={org}>{org}</option>}
            {orgSlugs.map((slug) => (
              <option key={slug} value={slug}>
                {slug}
              </option>
            ))}
          </select>
        </label>
        <StatusFilter value={state} onChange={(next) => go({ org, state: next, q })} options={STATE_OPTIONS} />
      </div>
      <QueryState query={projects} loading={<TableSkeleton />} errorTitle="Couldn't load the projects">
        {({ projects: all }) => (
          <ResponsiveTable
            columns={columns}
            rows={all.filter((p) => (!org || p.org === org) && inState(p, state) && (term === "" || projectMatches(p, term)))}
            rowKey={(p) => `${p.org}/${p.slug}`}
            rowTestId={(p) => `admin-project-${p.org}-${p.slug}`}
            onRowClick={(p) => void navigate({ to: "/admin/projects/$org/$slug", params: { org: p.org, slug: p.slug } })}
            empty={
              all.length === 0 ? (
                <EmptyState
                  title="No projects yet"
                  description="Projects appear here once an organization imports a repository from GitHub."
                />
              ) : (
                <p className="text-sm text-muted-foreground">No project matches.</p>
              )
            }
          />
        )}
      </QueryState>
    </PageContainer>
  );
}
