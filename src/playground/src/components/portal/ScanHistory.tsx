import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { HistoryIcon, SearchXIcon } from "lucide-react";
import { membersApi, portalApi, portalKey, projectApi, projectKey, type ScanRunRow } from "../../api";
import { canAdmin, isProduction, usePortalState } from "../../lib/identity";
import { formatUsd } from "../../lib/format";
import { timeAgo } from "../../lib/projectStatus";
import { SCAN_STATUSES, SCAN_TYPES, type ScansSearch, type ScanTrigger } from "../../lib/routeSearch";
import { useScanActions } from "../../lib/scanActions";
import { carriesCost, formatSeconds, requesterLabel, runOutcome, runSeconds, runStatus, runTitle } from "../../lib/scanFormat";
import { ErrorState } from "../state/ErrorState";
import { EmptyState } from "../state/EmptyState";
import { TableSkeleton } from "../state/Skeletons";
import { PageContainer } from "../layout/PageContainer";
import { ResponsiveTable, type Column } from "../layout/ResponsiveTable";
import { Button } from "../ui/button";
import { nativeSelect } from "./Field";
import { RunStatusBadge } from "./RunStatusBadge";
import { ScanMenu } from "./ScanMenu";

/** The Trigger filter's options; "Scheduled check" is two triggers (`poll` and `reconcile`). */
const TRIGGER_OPTIONS: { id: string; label: string; triggers: ScanTrigger[] }[] = [
  { id: "initial", label: "First scan or import", triggers: ["initial"] },
  { id: "manual", label: "Rescan", triggers: ["manual"] },
  { id: "describe", label: "Describe", triggers: ["describe"] },
  { id: "hook", label: "Commit hook", triggers: ["hook"] },
  { id: "push", label: "GitHub push", triggers: ["push"] },
  { id: "sync", label: "Sync from GitHub", triggers: ["sync"] },
  { id: "scheduled", label: "Scheduled check", triggers: ["poll", "reconcile"] },
];

const TYPE_LABEL = { full: "Full", quick: "Quick", sync: "Sync" } as const;

const triggerOptionOf = (triggers: ScanTrigger[] | undefined) =>
  triggers?.length ? (TRIGGER_OPTIONS.find((o) => o.triggers.join() === triggers.join())?.id ?? "") : "";

/** One filter: a labelled native select (a phone-friendly control) with an "Any" entry. */
function Filter({
  label,
  value,
  onChange,
  options,
  testId,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  testId: string;
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1 text-xs text-muted-foreground">
      {label}
      <select
        className={nativeSelect("w-full text-foreground sm:w-40")}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid={testId}
      >
        <option value="">Any</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * The project's runs, newest first, a page at a time: the run's title, who asked
 * for it, status, when, how long, what it cost (only when the payload carries
 * cost) and what it did. The whole row opens the run. The filters live in the URL
 * (`ScansSearch`) and go to the API; **Load more** follows `next`. Refreshes itself
 * while a run is active.
 */
export function ScanHistory({ slug }: { slug: string }) {
  const search = useSearch({ strict: false }) as ScansSearch;
  const navigate = useNavigate();
  const portal = usePortalState().data;
  const viewerUid = portal?.user?.uid ?? null;
  const production = isProduction(portal);
  const filters = {
    status: search.status,
    trigger: search.trigger,
    type: search.type,
    requester: search.requester,
  };
  const filtered = !!(filters.status?.length || filters.trigger?.length || filters.type || filters.requester);

  const runs = useInfiniteQuery({
    queryKey: [...projectKey(slug, "scans"), filters],
    queryFn: ({ pageParam }) => projectApi(slug).scans({ ...filters, before: pageParam }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.next ?? undefined,
    retry: false,
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.runs.some((r) => r.status === "queued" || r.status === "running"))
        ? 3000
        : false,
  });
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });
  // "Requested by" lists the org's members for those who manage them (production).
  const members = useQuery({
    queryKey: portalKey("members"),
    queryFn: membersApi.list,
    enabled: production && canAdmin(portal?.org?.role ?? undefined),
  });
  const { scanNow, scanPending } = useScanActions(slug);
  const list = runs.data?.pages.flatMap((p) => p.runs) ?? [];
  const usable = !!project.data?.initialized && project.data.root_status === "ok";
  const showCost = carriesCost(list);

  const setFilters = (next: ScansSearch) =>
    void navigate({
      to: "/p/$slug/scans/{-$runId}",
      params: { slug },
      search: { ...filters, ...next },
      replace: true,
    });

  const requesters = [
    ...(viewerUid ? [{ value: viewerUid, label: "Me" }] : []),
    { value: "system", label: "System" },
    ...(members.data ?? [])
      .filter((m) => m.uid !== viewerUid)
      .map((m) => ({ value: m.uid, label: m.display_name })),
  ];
  // A requester the URL names that is not listed (a removed member, a shared link).
  if (filters.requester && !requesters.some((r) => r.value === filters.requester)) {
    requesters.push({ value: filters.requester, label: "Someone else" });
  }

  const columns: Column<ScanRunRow>[] = [
    {
      key: "run",
      header: "Run",
      primary: true,
      cell: (run) => (
        <Link
          to="/p/$slug/scans/{-$runId}"
          params={{ slug, runId: String(run.id) }}
          onClick={(e) => e.stopPropagation()}
          className="text-primary-text hover:underline"
        >
          {runTitle(run)}
        </Link>
      ),
    },
    {
      key: "by",
      header: "Requested by",
      cell: (run) => <span className="text-muted-foreground">{requesterLabel(run, viewerUid)}</span>,
    },
    { key: "status", header: "Status", cell: (run) => <RunStatusBadge status={run.status} /> },
    {
      key: "when",
      header: "When",
      cell: (run) => (
        <span className="text-muted-foreground">
          {run.status === "queued"
            ? `Queued ${timeAgo(run.queued_at ?? null) ?? "just now"}`
            : (timeAgo(run.started_at) ?? "-")}
        </span>
      ),
    },
    {
      key: "duration",
      header: "Duration",
      cell: (run) => {
        const seconds = runSeconds(run);
        return <span className="tabular-nums text-muted-foreground">{seconds !== null ? formatSeconds(seconds) : "-"}</span>;
      },
    },
    ...(showCost
      ? [
          {
            key: "cost",
            header: "Cost",
            cell: (run: ScanRunRow) => (
              <span className="tabular-nums">
                {typeof run.cost_usd === "number" && run.cost_usd > 0 ? formatUsd(run.cost_usd) : "-"}
                {typeof run.estimate_usd === "number" && (
                  <span className="block text-xs text-muted-foreground" data-testid="run-estimate">
                    est. {formatUsd(run.estimate_usd)}
                  </span>
                )}
              </span>
            ),
          },
        ]
      : []),
    {
      key: "result",
      header: "Result",
      cell: (run) => {
        const outcome = runOutcome(run, viewerUid);
        return (
          <span className="block max-w-56 truncate text-xs text-muted-foreground sm:max-w-48" title={outcome ?? ""}>
            {outcome ?? ""}
          </span>
        );
      },
    },
  ];

  const empty =
    runs.isSuccess && list.length === 0 ? (
      filtered ? (
        <EmptyState
          icon={<SearchXIcon />}
          title="No runs match these filters"
          description="Try fewer filters, or clear them to see every run."
          action={
            <Button size="sm" variant="outline" onClick={() => setFilters({ status: undefined, trigger: undefined, type: undefined, requester: undefined })}>
              Clear filters
            </Button>
          }
          className="border border-dashed border-border py-14"
        />
      ) : (
        <EmptyState
          icon={<HistoryIcon />}
          title="No scans yet"
          description="Scan now to index this project's history and code structure."
          action={
            project.data && <ScanMenu project={project.data} onScan={scanNow} disabled={!usable || scanPending} />
          }
          className="border border-dashed border-border py-14"
        />
      )
    ) : null;

  return (
    <PageContainer width="default" className="flex flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">Scans</h1>
          <p className="text-[13px] text-muted-foreground">Every scan and sync for this project, newest first.</p>
        </div>
        {project.data && !(list.length === 0 && runs.isSuccess && !filtered) && (
          <div className="flex gap-2">
            <ScanMenu project={project.data} onScan={scanNow} disabled={!usable || scanPending} />
          </div>
        )}
      </div>

      {(list.length > 0 || filtered) && (
        <div className="grid grid-cols-2 gap-3 sm:flex sm:flex-wrap sm:items-end" data-testid="scan-filters">
          <Filter
            label="Status"
            testId="filter-status"
            value={filters.status?.[0] ?? ""}
            onChange={(v) => setFilters({ status: v ? [v as (typeof SCAN_STATUSES)[number]] : undefined })}
            options={SCAN_STATUSES.map((s) => ({ value: s, label: runStatus(s).label }))}
          />
          <Filter
            label="Trigger"
            testId="filter-trigger"
            value={triggerOptionOf(filters.trigger)}
            onChange={(v) => setFilters({ trigger: TRIGGER_OPTIONS.find((o) => o.id === v)?.triggers })}
            options={TRIGGER_OPTIONS.map((o) => ({ value: o.id, label: o.label }))}
          />
          <Filter
            label="Requested by"
            testId="filter-requester"
            value={filters.requester ?? ""}
            onChange={(v) => setFilters({ requester: v || undefined })}
            options={requesters}
          />
          <Filter
            label="Type"
            testId="filter-type"
            value={filters.type ?? ""}
            onChange={(v) => setFilters({ type: (v || undefined) as ScansSearch["type"] })}
            options={SCAN_TYPES.map((t) => ({ value: t, label: TYPE_LABEL[t] }))}
          />
          {filtered && (
            <Button
              size="sm"
              variant="ghost"
              className="justify-self-start"
              onClick={() => setFilters({ status: undefined, trigger: undefined, type: undefined, requester: undefined })}
            >
              Clear filters
            </Button>
          )}
        </div>
      )}

      {runs.isLoading && <TableSkeleton rows={6} cols={5} label="Loading scans" />}
      {runs.isError && (
        <ErrorState
          error={runs.error}
          title="Couldn't load scans"
          onRetry={() => void runs.refetch()}
          size="section"
        />
      )}
      {empty}
      {list.length > 0 && (
        <div data-testid="scan-history" className="flex flex-col gap-3">
          <ResponsiveTable
            columns={columns}
            rows={list}
            rowKey={(run) => String(run.id)}
            rowTestId={(run) => `run-${run.id}`}
            onRowClick={(run) =>
              void navigate({ to: "/p/$slug/scans/{-$runId}", params: { slug, runId: String(run.id) } })
            }
          />
          {runs.hasNextPage && (
            <div className="flex justify-center">
              <Button
                variant="outline"
                size="sm"
                onClick={() => void runs.fetchNextPage()}
                disabled={runs.isFetchingNextPage}
                data-testid="scans-load-more"
              >
                {runs.isFetchingNextPage ? "Loading..." : "Load more"}
              </Button>
            </div>
          )}
        </div>
      )}
    </PageContainer>
  );
}
