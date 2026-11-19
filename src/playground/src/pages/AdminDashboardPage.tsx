import { useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import {
  adminApi,
  type AdminAttention,
  type AdminKpi,
  type AdminStatsOverview,
  type AdminStatsSeries,
  type AdminStatsTop,
  type AdminTopMetric,
} from "../api";
import { AdminChart } from "../components/admin/AdminChart";
import { AdminHeading } from "../components/admin/AdminHeading";
import type { ChartPayload } from "../components/charts/chartSpec";
import { readStack } from "../components/charts/chartSpec";
import { PageContainer } from "../components/layout/PageContainer";
import { nativeSelect } from "../components/portal/Field";
import { ErrorState } from "../components/state/ErrorState";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Skeleton } from "../components/ui/skeleton";
import { RangePicker, StatTile, UsageSection } from "../components/usage/parts";
import {
  formatBytes,
  formatDateUtc,
  formatNumber,
  formatTokens,
  formatUsd,
  timeAgo,
} from "../lib/format";
import type { AdminDashboardSearch, AdminRank } from "../lib/routeSearch";
import { matchPreset, monthRange, presetRange, type UsageRange } from "../lib/usageRange";

const sign = (n: number) => (n < 0 ? "-" : "+");

/** "+3 since Nov 1" (a plain hyphen for a minus). */
function countChange(kpi: AdminKpi, from: string): string | undefined {
  if (kpi.previous === null) return undefined;
  const delta = kpi.value - kpi.previous;
  return `${sign(delta)}${formatNumber(Math.abs(delta))} since ${formatDateUtc(from)}`;
}

function storageChange(kpi: AdminKpi, from: string): string | undefined {
  if (kpi.previous === null) return undefined;
  const delta = kpi.value - kpi.previous;
  return `${sign(delta)}${formatBytes(Math.abs(delta))} since ${formatDateUtc(from)}. Purged projects are not counted.`;
}

function callsChange(kpi: AdminKpi): string | undefined {
  if (kpi.previous === null) return undefined;
  if (kpi.previous === 0) return kpi.value === 0 ? undefined : "No calls in the 7 days before";
  const pct = Math.round(((kpi.value - kpi.previous) / kpi.previous) * 100);
  return `${sign(pct)}${Math.abs(pct)}% on the 7 days before`;
}

/** The seven tiles, all from the one overview call. */
function Tiles({ data }: { data: AdminStatsOverview }) {
  const { kpis, from } = data;
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4" data-testid="admin-tiles">
      <StatTile label="Users" value={formatNumber(kpis.users.value)} detail={countChange(kpis.users, from)} testId="tile-users" />
      <StatTile label="Organizations" value={formatNumber(kpis.orgs.value)} detail={countChange(kpis.orgs, from)} testId="tile-orgs" />
      <StatTile label="Projects" value={formatNumber(kpis.projects.value)} testId="tile-projects" />
      <StatTile
        label="LLM spend MTD"
        value={formatUsd(kpis.llm_spend_mtd.value)}
        detail={
          kpis.llm_spend_mtd.previous === null
            ? undefined
            : `${formatUsd(kpis.llm_spend_mtd.previous)} last month by this day`
        }
        testId="tile-spend"
      />
      <StatTile label="Tokens MTD" value={formatTokens(kpis.tokens_mtd.value)} testId="tile-tokens" />
      <StatTile
        label="Storage"
        value={formatBytes(kpis.storage_bytes.value)}
        detail={storageChange(kpis.storage_bytes, from)}
        testId="tile-storage"
      />
      <StatTile
        label="Agent calls (7 d)"
        value={formatNumber(kpis.agent_calls_7d.value)}
        detail={callsChange(kpis.agent_calls_7d)}
        testId="tile-agent-calls"
      />
    </div>
  );
}

/** One attention line: its words (server strings are text nodes) and where it leads. */
function attentionLine(item: AdminAttention): ReactNode {
  const org = item.org?.name ?? "";
  const project = item.project;
  const toProject = (label: string) =>
    project ? (
      <Link to="/admin/projects/$org/$slug" params={{ org: project.org, slug: project.slug }} className="text-primary-text hover:underline">
        {label}
      </Link>
    ) : (
      label
    );
  const toOrg = (label: string, tab?: "usage") =>
    item.org ? (
      <Link
        to="/admin/orgs/$slug"
        params={{ slug: item.org.slug }}
        search={tab ? { tab } : {}}
        className="text-primary-text hover:underline"
      >
        {label}
      </Link>
    ) : (
      label
    );
  const toUser = (label: string) =>
    item.user ? (
      <Link to="/admin/users/$uid" params={{ uid: item.user.uid }} className="text-primary-text hover:underline">
        {label}
      </Link>
    ) : (
      label
    );
  switch (item.kind) {
    case "scan_failed":
      return (
          <>
            {toProject(`${project?.name ?? ""} in ${project ? orgOf(item) : ""}`)}: scan failed {timeAgo(item.at ?? null) ?? ""}
          </>
        );
    case "access_lost":
      return <>{toProject(`${project?.name ?? ""} in ${orgOf(item)}`)}: GitHub access lost</>;
    case "budget_stopped":
      return <>{toOrg(org, "usage")}: monthly budget reached</>;
    case "base_check":
      return (
        <Link to="/admin/config" className="text-primary-text hover:underline">
          {item.text ?? ""}
        </Link>
      );
    case "update":
      return item.url && item.url.startsWith("https://") ? (
            <a href={item.url} target="_blank" rel="noreferrer" className="text-primary-text hover:underline">
              WhyGraph {item.text ?? ""} is available
            </a>
          ) : (
            <>WhyGraph {item.text ?? ""} is available</>
          );
    case "password_owner":
      return (
          <>
            {toUser(item.user?.label ?? "")} (password account) owns {formatNumber(item.count ?? 0)}{" "}
            {item.count === 1 ? "organization" : "organizations"}: hand them to a GitHub user
          </>
        );
    case "password_not_admin":
      return <>{toUser(item.user?.label ?? "")} can&apos;t sign in (password account, not an administrator)</>;
    case "org_suspended":
      return <>{toOrg(org)} is suspended</>;
    case "deleted_with_data": {
      const label = project ? `${project.name} in ${orgOf(item)}` : org;
      return (
          <>
            {project ? toProject(label) : toOrg(label)} is deleted and still holds {formatBytes(item.bytes ?? 0)}
          </>
        );
    }
  }
}

/** The org a project line names (the item's own `org` block, else the project's org slug). */
function orgOf(item: AdminAttention): string {
  return item.org?.name ?? item.project?.org ?? "";
}

function Attention({ items }: { items: AdminAttention[] }) {
  if (items.length === 0) {
    return (
      <Alert variant="success" data-testid="attention-none">
        <AlertDescription>Nothing needs your attention.</AlertDescription>
      </Alert>
    );
  }
  return (
    <UsageSection title="Needs attention" testId="attention">
      <ul className="flex flex-col gap-1.5 text-sm">
        {items.map((item, i) => (
          <li key={`${item.kind}:${i}`} data-testid={`attention-${item.kind}`}>
            {attentionLine(item)}
          </li>
        ))}
      </ul>
    </UsageSection>
  );
}

const RANKS: { value: AdminRank | "spend"; label: string; noun: string; format: "usd" | "bytes" | "number"; y: string }[] = [
  { value: "spend", label: "Spend", noun: "spend", format: "usd", y: "Cost (USD)" },
  { value: "storage", label: "Storage", noun: "storage", format: "bytes", y: "Storage" },
  { value: "agent_calls", label: "Agent calls", noun: "agent calls", format: "number", y: "Agent calls" },
];

function stackedPayload(data: AdminStatsSeries, title: string, yLabel: string): ChartPayload | null {
  if (data.rows.length === 0) return null;
  const stack = data.stack ? readStack(data.stack) : null;
  if (!stack) return null;
  return {
    kind: "bar_stacked",
    title,
    yLabel,
    xIndex: 0,
    yIndex: data.columns.length - 1,
    columns: data.columns,
    rows: data.rows,
    nullRows: 0,
    stack,
  };
}

function ChartSkeleton() {
  return <Skeleton className="h-[260px]" aria-label="Loading chart" />;
}

function SeriesChart({
  metric,
  from,
  to,
  title,
  yLabel,
  format,
  empty,
  testId,
}: {
  metric: "spend" | "scans";
  from: string;
  to: string;
  title: string;
  yLabel: string;
  format: "usd" | "number";
  empty: string;
  testId: string;
}) {
  const query = useQuery({
    queryKey: ["@admin", "stats", "series", metric, from, to],
    queryFn: () => adminApi.statsSeries(metric, from, to),
  });
  const payload = useMemo(() => (query.data ? stackedPayload(query.data, title, yLabel) : null), [query.data, title, yLabel]);
  return (
    <UsageSection title={title} testId={`${testId}-section`}>
      {query.isError ? (
        <ErrorState error={query.error} title={`Couldn't load "${title}"`} onRetry={() => void query.refetch()} />
      ) : query.data === undefined ? (
        <ChartSkeleton />
      ) : (
        <AdminChart payload={payload} valueFormat={format} empty={empty} testId={testId} />
      )}
    </UsageSection>
  );
}

function TopChart({ rank, from, to, onRank }: { rank: AdminTopMetric; from: string; to: string; onRank: (rank: AdminTopMetric) => void }) {
  const query = useQuery({
    queryKey: ["@admin", "stats", "top", rank, from, to],
    queryFn: () => adminApi.statsTop(rank, from, to),
  });
  const meta = RANKS.find((r) => r.value === rank) ?? RANKS[0];
  const title = `Top 10 organizations by ${meta.noun}`;
  const payload = useMemo(() => (query.data ? topPayload(query.data, title, meta.y) : null), [query.data, title, meta.y]);
  return (
    <UsageSection
      title={title}
      testId="top-section"
      actions={
        <label className="flex flex-col gap-1 text-xs font-medium sm:w-44">
          Rank by
          <select
            className={nativeSelect("font-normal")}
            value={rank}
            onChange={(e) => onRank(e.target.value as AdminTopMetric)}
            data-testid="rank-select"
          >
            {RANKS.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
      }
    >
      {query.isError ? (
        <ErrorState error={query.error} title="Couldn't load the ranking" onRetry={() => void query.refetch()} />
      ) : query.data === undefined ? (
        <ChartSkeleton />
      ) : (
        <AdminChart payload={payload} valueFormat={meta.format} empty="Nothing to rank in this range." testId="top-chart" />
      )}
    </UsageSection>
  );
}

function topPayload(data: AdminStatsTop, title: string, yLabel: string): ChartPayload | null {
  if (data.items.length === 0) return null;
  return {
    kind: "bar_h",
    title,
    yLabel,
    xIndex: 0,
    yIndex: 1,
    columns: ["organization", data.metric],
    rows: data.items.map((i) => [i.name, i.value]),
    nullRows: 0,
  };
}

/** `/admin`: what needs attention, seven tiles and three charts, then the address check and the update status. */
export function AdminDashboardPage() {
  const search = useSearch({ strict: false }) as AdminDashboardSearch;
  const navigate = useNavigate();
  const rank: AdminTopMetric = search.rank ?? "spend";
  // The page opens on 30 days and always sends both bounds (the server's default is the month).
  const range: UsageRange = useMemo(
    () => (search.from && search.to ? { from: search.from, to: search.to } : presetRange("30d")),
    [search.from, search.to],
  );
  const go = (next: AdminDashboardSearch) => void navigate({ to: "/admin", search: next, replace: true });
  const setRange = (r: Partial<UsageRange>) => {
    // The picker's "This month" is an empty range; here it is the month's own bounds.
    const next = r.from && r.to ? { from: r.from, to: r.to } : monthRange(0);
    go({ ...(matchPreset(next) === "30d" ? {} : next), rank: search.rank });
  };
  const overview = useQuery({
    queryKey: ["@admin", "stats", "overview", range.from, range.to],
    queryFn: () => adminApi.statsOverview(range.from, range.to),
  });
  return (
    <PageContainer width="wide" className="flex flex-col gap-6">
      <div className="row-wrap">
        <div className="flex-1">
          <AdminHeading>Dashboard</AdminHeading>
        </div>
        <RangePicker range={range} onChange={setRange} />
      </div>
      {overview.isError ? (
        <ErrorState
          error={overview.error}
          title="Couldn't load the platform figures"
          onRetry={() => void overview.refetch()}
        />
      ) : overview.data === undefined ? (
        <Skeleton className="h-32" aria-label="Loading the platform figures" />
      ) : (
        <>
          <Attention items={overview.data.attention} />
          <Tiles data={overview.data} />
        </>
      )}
      <div className="grid gap-6 lg:grid-cols-2">
        <SeriesChart
          metric="spend"
          from={range.from}
          to={range.to}
          title="LLM spend per day"
          yLabel="Cost (USD)"
          format="usd"
          empty="No LLM spend in this range."
          testId="spend-chart"
        />
        <SeriesChart
          metric="scans"
          from={range.from}
          to={range.to}
          title="Scans per day"
          yLabel="Runs"
          format="number"
          empty="No scans in this range."
          testId="scans-chart"
        />
      </div>
      <TopChart
        rank={rank}
        from={range.from}
        to={range.to}
        onRank={(next) => go({ from: search.from, to: search.to, rank: next === "spend" ? undefined : next })}
      />
    </PageContainer>
  );
}
