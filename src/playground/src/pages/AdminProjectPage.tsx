import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { toast } from "sonner";
import { ApiError, adminApi, type AdminProjectDetail, type ScanRunRow } from "../api";
import { AdminChart } from "../components/admin/AdminChart";
import { AdminHeading } from "../components/admin/AdminHeading";
import { ADMIN, AdminNotFound, DeletedAlert, hasStatus, toastError } from "../components/admin/adminParts";
import { ProjectActions, projectState } from "../components/admin/ProjectActions";
import type { ChartPayload } from "../components/charts/chartSpec";
import { PageContainer } from "../components/layout/PageContainer";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { RunStatusBadge } from "../components/portal/RunStatusBadge";
import { DisabledReason } from "../components/state/DisabledReason";
import { ErrorState } from "../components/state/ErrorState";
import { FormSkeleton } from "../components/state/Skeletons";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { StatTile, UsageSection } from "../components/usage/parts";
import { formatBytes, formatDateTime, formatNumber, formatUsd, timeAgo } from "../lib/format";
import { formatSeconds, requesterLabel, runSeconds, runTitle } from "../lib/scanFormat";

const active = (run: Pick<ScanRunRow, "status">) => run.status === "queued" || run.status === "running";

/** The tail of one run's log, as a text node (the server's text is never markup). */
function RunLog({ org, slug, run }: { org: string; slug: string; run: ScanRunRow }) {
  const log = useQuery({
    // `status` is part of the key so the log is read once more after the run ends.
    queryKey: [...ADMIN, "project", org, slug, "log", run.id, run.status],
    queryFn: () => adminApi.projectRunLog(org, slug, run.id),
    refetchInterval: active(run) ? 2000 : false,
    retry: false,
  });
  return (
    <section className="flex flex-col gap-2 rounded-xl border border-border bg-card px-5 py-3 shadow-card" data-testid="run-log">
      <h3 className="text-sm font-semibold">Log: {runTitle(run)}</h3>
      {log.isLoading && <p className="text-xs text-muted-foreground">Loading the log...</p>}
      {log.isError && <ErrorState error={log.error} size="inline" onRetry={() => void log.refetch()} />}
      {log.data && (
        <>
          {log.data.truncated && (
            <p className="text-xs text-muted-foreground">
              Showing the end of the log ({formatNumber(Math.round(log.data.size / 1024))} KiB in all). Keys are redacted.
            </p>
          )}
          <pre
            className="max-h-72 overflow-auto rounded-md bg-muted p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all"
            data-testid="run-log-text"
          >
            {log.data.text || "No output yet."}
          </pre>
        </>
      )}
    </section>
  );
}

function Scans({ project, writable }: { project: AdminProjectDetail; writable: boolean }) {
  const queryClient = useQueryClient();
  const [logRun, setLogRun] = useState<number | null>(null);
  const cancel = useMutation({
    mutationFn: (runId: number) => adminApi.cancelProjectRun(project.org, project.slug, runId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ADMIN }),
    onError: toastError,
  });
  const columns: Column<ScanRunRow>[] = [
    { key: "run", header: "Run", primary: true, cell: (r) => runTitle(r) },
    { key: "status", header: "Status", cell: (r) => <RunStatusBadge status={r.status} /> },
    { key: "by", header: "Requested by", cell: (r) => <span className="text-muted-foreground">{requesterLabel(r)}</span> },
    {
      key: "queued",
      header: "Queued",
      cell: (r) => <span className="text-muted-foreground">{timeAgo(r.queued_at ?? r.started_at ?? null) ?? "-"}</span>,
    },
    {
      key: "duration",
      header: "Duration",
      cell: (r) => {
        const seconds = runSeconds(r);
        return <span className="tabular-nums text-muted-foreground">{seconds !== null ? formatSeconds(seconds) : "-"}</span>;
      },
    },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      cell: (r) => (
        <span className="flex flex-wrap items-center gap-2">
          {writable && active(r) && (
            <Button size="sm" variant="outline" disabled={cancel.isPending} onClick={() => cancel.mutate(r.id)}>
              Cancel
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={logRun === r.id}
            onClick={() => setLogRun(logRun === r.id ? null : r.id)}
          >
            {logRun === r.id ? "Hide log" : "Log"}
          </Button>
        </span>
      ),
    },
  ];
  const open = project.runs.find((r) => r.id === logRun);
  return (
    <div className="flex flex-col gap-3" data-testid="project-scans">
      <h2 className="text-sm font-semibold">Scans</h2>
      <ResponsiveTable
        columns={columns}
        rows={project.runs}
        rowKey={(r) => String(r.id)}
        rowTestId={(r) => `project-run-${r.id}`}
        empty={<p className="text-sm text-muted-foreground">This project has no scans yet.</p>}
      />
      {open && <RunLog org={project.org} slug={project.slug} run={open} />}
    </div>
  );
}

function storagePayloads(project: AdminProjectDetail): { parts: ChartPayload | null; total: ChartPayload | null } {
  const { latest, series } = project.storage;
  const parts: ChartPayload | null = latest
    ? {
        kind: "bar_h",
        title: "Storage by part",
        yLabel: "Storage",
        xIndex: 0,
        yIndex: 1,
        columns: ["part", "bytes"],
        rows: [
          ["History database", latest.history_bytes],
          ["Git", latest.git_bytes],
          ["Code index", latest.codegraph_bytes],
          ["Working tree", latest.worktree_bytes],
          ["Run logs", latest.runs_bytes],
        ],
        nullRows: 0,
      }
    : null;
  const total: ChartPayload | null =
    series.rows.length > 0
      ? {
          kind: "line",
          title: "Total storage, last 90 days",
          yLabel: "Storage",
          xIndex: 0,
          yIndex: 1,
          columns: series.columns,
          rows: series.rows,
          nullRows: 0,
        }
      : null;
  return { parts, total };
}

function Storage({ project }: { project: AdminProjectDetail }) {
  const { parts, total } = storagePayloads(project);
  return (
    <div className="grid gap-6 lg:grid-cols-2" data-testid="project-storage">
      <UsageSection
        title="Storage by part"
        description={project.storage.latest ? `${formatBytes(project.storage.latest.total_bytes)} in all` : undefined}
      >
        <AdminChart payload={parts} valueFormat="bytes" empty="Not measured yet." testId="storage-parts-chart" />
      </UsageSection>
      <UsageSection title="Total storage, last 90 days">
        <AdminChart payload={total} valueFormat="bytes" empty="Not measured yet." testId="storage-total-chart" />
        <p className="text-xs text-muted-foreground">Purged projects are not shown.</p>
      </UsageSection>
    </div>
  );
}

function Detail({ project }: { project: AdminProjectDetail }) {
  const queryClient = useQueryClient();
  const orgs = useQuery({ queryKey: [...ADMIN, "orgs"], queryFn: adminApi.orgs });
  const org = orgs.data?.find((o) => o.slug === project.org);
  const state = projectState({
    deleted_at: project.deleted_at,
    access_lost_reason: project.access_lost_reason,
    last_run_status: project.runs.find((r) => !active(r))?.status ?? null,
    last_scan_at: project.last_scan_at,
  });
  const done = () => queryClient.invalidateQueries({ queryKey: ADMIN });
  const rescan = useMutation({
    mutationFn: () => adminApi.rescanProject(project.org, project.slug),
    onSuccess: async () => {
      toast.success("Rescan queued");
      await done();
    },
    onError: toastError,
  });
  const measure = useMutation({
    mutationFn: () => adminApi.measureProjectStorage(project.org, project.slug),
    onSuccess: async (res) => {
      if (res.measured) toast.success("Storage measured");
      else toast.warning("Nothing to measure", { description: "This project has no server copy to measure." });
      await done();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409 && err.code === "busy") {
        toast.error("Another storage measurement is running. Try again in a few minutes.");
      } else toastError(err);
    },
  });
  const gone = !!project.deleted_at || !!project.org_deleted_at;
  const suspended = !!project.org_suspended;
  const { usage, agents, last_failure: failure } = project.overview;
  const rescanButton = (
    <Button disabled={rescan.isPending || suspended} onClick={() => rescan.mutate()}>
      Rescan
    </Button>
  );
  return (
    <PageContainer width="default" className="flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <AdminHeading
          description={
            <>
              <Link to="/admin/orgs/$slug" params={{ slug: project.org }} className="hover:underline">
                {org?.name ?? project.org}
              </Link>{" "}
              / <span className="font-mono">{project.slug}</span>
            </>
          }
        >
          {project.name}
        </AdminHeading>
        <div>
          <Badge variant={state.variant}>{state.label}</Badge>
        </div>
      </div>
      {project.deleted_at && <DeletedAlert deletedAt={project.deleted_at} deletedBy={null} deletedByName={project.deleted_by} />}
      {!project.deleted_at && project.org_deleted_at && (
        <Alert variant="warning" data-testid="org-deleted-alert">
          <AlertDescription>The organization is deleted. Restore it before changing this project.</AlertDescription>
        </Alert>
      )}
      {project.access_lost_reason && !project.deleted_at && (
        <Alert variant="warning" data-testid="access-lost-alert">
          <AlertDescription>GitHub access lost: {project.access_lost_reason}</AlertDescription>
        </Alert>
      )}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3" data-testid="project-tiles">
        <StatTile
          label="Spend this month"
          value={usage ? formatUsd(usage.spent_usd) : "-"}
          detail={usage?.budget_usd != null ? `of ${formatUsd(usage.budget_usd)}` : undefined}
          testId="tile-project-spend"
        />
        <StatTile label="Agent calls" value={formatNumber(agents.total_calls)} testId="tile-project-calls" />
        <StatTile label="Last scan" value={timeAgo(project.last_scan_at) ?? "Never"} testId="tile-project-scan" />
      </div>
      {failure && !project.deleted_at && (
        <Alert variant="destructive" data-testid="last-failure">
          <AlertDescription>
            Last scan failed {formatDateTime(failure.at)}: {failure.message}
          </AlertDescription>
        </Alert>
      )}
      <div className="row-wrap" data-testid="project-actions">
        {!gone && (
          <>
            {suspended ? (
              <DisabledReason reason="The organization is suspended.">{rescanButton}</DisabledReason>
            ) : (
              rescanButton
            )}
            <Button variant="outline" disabled={measure.isPending} onClick={() => measure.mutate()}>
              {measure.isPending ? "Measuring..." : "Measure now"}
            </Button>
          </>
        )}
        <ProjectActions
          org={{ slug: project.org, name: org?.name ?? project.org, deleted_at: project.org_deleted_at }}
          project={project}
        />
      </div>
      <Scans project={project} writable={!gone && !suspended} />
      <Storage project={project} />
    </PageContainer>
  );
}

/** `/admin/projects/$org/$slug`: one project with its figures, scans and storage. */
export function AdminProjectPage() {
  const { org, slug } = useParams({ strict: false }) as { org: string; slug: string };
  const project = useQuery({
    queryKey: [...ADMIN, "project", org, slug],
    queryFn: () => adminApi.project(org, slug),
    retry: false,
    // A queued or running scan moves on its own.
    refetchInterval: (q) => (q.state.data?.runs.some(active) ? 5000 : false),
  });
  if (project.data === undefined) {
    if (hasStatus(project.error, 404)) return <AdminNotFound label="Back to projects" to="/admin/projects" />;
    if (project.isError) {
      return (
        <PageContainer width="narrow">
          <ErrorState error={project.error} title="Couldn't load the project" onRetry={() => void project.refetch()} />
        </PageContainer>
      );
    }
    return (
      <PageContainer width="default">
        <FormSkeleton />
      </PageContainer>
    );
  }
  return <Detail project={project.data} />;
}
