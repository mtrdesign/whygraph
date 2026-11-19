import { useQuery } from "@tanstack/react-query";
import { useSearch } from "@tanstack/react-router";
import { adminApi, type AdminOrg } from "../api";
import { AdminHeading } from "../components/admin/AdminHeading";
import { orgMatches } from "../components/admin/AdminCommandPalette";
import { AdminSearchBox } from "../components/admin/AdminSearchBox";
import { PageContainer } from "../components/layout/PageContainer";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { QueryState } from "../components/state/QueryState";
import { TableSkeleton } from "../components/state/Skeletons";
import { formatDate } from "../lib/format";
import { plural } from "../lib/plural";
import { safeHref } from "../lib/platformLink";

const columns: Column<AdminOrg>[] = [
  {
    key: "org",
    header: "Organization",
    primary: true,
    cell: (o) => (
      <span className="flex min-w-0 flex-col leading-tight">
        <span className="truncate font-medium">{o.name}</span>
        <span className="truncate font-mono text-xs font-normal text-muted-foreground">{o.slug}</span>
      </span>
    ),
  },
  {
    key: "url",
    header: "Address",
    hideBelow: "md",
    cell: (o) => {
      const href = safeHref(o.url);
      return href ? (
        <a href={href} className="break-all text-primary-text hover:underline">
          {o.url}
        </a>
      ) : (
        <span className="break-all">{o.url}</span>
      );
    },
  },
  { key: "members", header: "Members", align: "right", cell: (o) => plural(o.member_count, "member") },
  { key: "created", header: "Created", hideBelow: "md", cell: (o) => formatDate(o.created_at) },
];

/** `/admin/orgs`: every organization (the full module is Phase 2). */
export function AdminOrgsPage() {
  const { q } = useSearch({ strict: false }) as { q?: string };
  const orgs = useQuery({ queryKey: ["@admin", "orgs"], queryFn: adminApi.orgs });
  const term = (q ?? "").trim().toLowerCase();
  return (
    <PageContainer width="wide" className="flex flex-col gap-4">
      <AdminHeading>Organizations</AdminHeading>
      <AdminSearchBox to="/admin/orgs" q={q} />
      <QueryState query={orgs} loading={<TableSkeleton />} errorTitle="Couldn't load the organizations">
        {(all) => (
          <ResponsiveTable
            columns={columns}
            rows={all.filter((o) => term === "" || orgMatches(o, term))}
            rowKey={(o) => o.slug}
            rowTestId={(o) => `admin-org-${o.slug}`}
            empty={
              <p className="text-sm text-muted-foreground">
                {all.length === 0
                  ? "No organizations yet. People create them after signing in with GitHub."
                  : "No organization matches that search."}
              </p>
            }
          />
        )}
      </QueryState>
    </PageContainer>
  );
}
