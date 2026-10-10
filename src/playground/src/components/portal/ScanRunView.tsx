import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2Icon,
  CircleDashedIcon,
  CircleIcon,
  MinusCircleIcon,
  SquareIcon,
  XCircleIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { ApiError, portalApi, projectApi, projectKey, type ScanRunRow, type ScanRunStatus } from "../../api";
import { usePortalState } from "../../lib/identity";
import { can } from "../../lib/permissions";
import { projectProblem } from "../../lib/errors";
import { formatClock, formatNumber, formatUsd } from "../../lib/format";
import { useScanActions } from "../../lib/scanActions";
import { scanAvailability } from "../../lib/scanAvailability";
import {
  BUDGET_SKIPPED,
  CODE_INDEX_REFRESHED,
  cancelledLabel,
  failureSummary,
  formatSeconds,
  requesterLabel,
  runSeconds,
  runTitle,
  syncOutcome,
  usageLine,
} from "../../lib/scanFormat";
import {
  codegraphRow,
  crawlerLabel,
  phaseRows,
  useScanRun,
  type PhaseRow,
  type PhaseStatus,
  type ScanRunState,
  type TaskState,
} from "../../lib/scanRun";
import { PageContainer } from "../layout/PageContainer";
import { scanRunKey } from "../shell/crumbs";
import { ErrorState } from "../state/ErrorState";
import { NotFoundState } from "../state/NotFoundState";
import { DetailSkeleton } from "../state/Skeletons";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Progress } from "../ui/progress";
import { Spinner } from "../ui/spinner";
import { CancelRunButton } from "./CancelRunButton";
import { FULL_SCAN, QUICK_SCAN, ScanMenu } from "./ScanMenu";
import { RunStatusBadge } from "./RunStatusBadge";

/** Re-render every `ms` while `enabled` (a live elapsed-time readout). */
function useNow(ms: number, enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms, enabled]);
  return now;
}

const PHASE_ICON: Record<PhaseStatus, React.ReactNode> = {
  pending: <CircleIcon className="size-4 text-muted-foreground/60" />,
  running: <Spinner className="size-4 text-primary" />,
  done: <CheckCircle2Icon className="size-4 text-success" />,
  failed: <XCircleIcon className="size-4 text-destructive" />,
  skipped: <MinusCircleIcon className="size-4 text-muted-foreground/60" />,
  // A stopped phase is not an error: a grey stop square, not the red X (SCN-6).
  cancelled: <SquareIcon className="size-3.5 fill-current text-muted-foreground/70" />,
};

function TaskLine({ task, showLabel = true }: { task: TaskState; showLabel?: boolean }) {
  const pct = task.total ? Math.min(100, Math.round((task.completed / task.total) * 100)) : null;
  return (
    <div className="flex flex-col gap-1" data-testid={`task-${task.name}`}>
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="font-medium">{showLabel ? crawlerLabel(task.name) : ""}</span>
        <span className="truncate text-muted-foreground">
          {task.description}
          {task.total ? (
            <span className="ml-2 tabular-nums text-foreground">
              {formatNumber(task.completed)} / {formatNumber(task.total)}
            </span>
          ) : null}
        </span>
      </div>
      {pct !== null && <Progress value={pct} aria-label={crawlerLabel(task.name)} />}
    </div>
  );
}

function PhaseItem({ phase }: { phase: PhaseRow }) {
  const reported = phase.crawlers.filter((c) => c.summary || c.error || c.warning);
  // While a phase runs its crawlers report live; afterwards their final summary
  // replaces the moving description.
  const showTasks = phase.status === "running" || (phase.status !== "pending" && reported.length === 0);
  return (
    <li className="flex gap-3 py-3" data-testid={`phase-${phase.phase}`} data-status={phase.status}>
      <span className="mt-0.5 shrink-0" aria-hidden>
        {PHASE_ICON[phase.status]}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex items-baseline justify-between gap-3">
          <span className={cn(
              "text-sm font-medium",
              (phase.status === "pending" || phase.status === "skipped") && "text-muted-foreground",
            )}>
            {phase.title}
          </span>
          {phase.seconds !== null && (
            <span className="text-xs tabular-nums text-muted-foreground">{formatSeconds(phase.seconds)}</span>
          )}
          {(phase.status === "skipped" || phase.status === "cancelled") && (
            <span className="text-xs text-muted-foreground">{phase.status === "skipped" ? "Skipped" : "Stopped"}</span>
          )}
        </div>
        {showTasks && phase.tasks.map((t) => (
            <TaskLine key={t.name} task={t} showLabel={crawlerLabel(t.name) !== phase.title} />
          ))}
        {phase.status !== "running" &&
          reported.map((c) => (
            <p
              key={c.name}
              className={cn("text-xs", c.error ? "text-destructive" : "text-muted-foreground")}
            >
              <span className="font-medium text-foreground">{crawlerLabel(c.name)}</span>{" "}
              {c.error ?? c.summary}
              {c.warning && <span className="block text-warning">{c.warning}</span>}
            </p>
          ))}
      </div>
    </li>
  );
}

function SyncItem({ sync }: { sync: NonNullable<ScanRunState["sync"]> }) {
  const failed = sync.status === "failed";
  const text =
    sync.status === "cloning"
      ? `Cloning ${sync.fullName ?? "the repository"}`
      : sync.status === "fetching"
        ? "Fetching from GitHub"
        : failed
          ? (sync.error ?? "The fetch did not finish")
          : sync.cloned
            ? "Cloned the repository"
            : sync.moved
              ? "Fetched new commits; scanning them next"
              : "No new commits; nothing to scan";
  const status: PhaseStatus =
    sync.status === "fetching" || sync.status === "cloning" ? "running" : failed ? "failed" : "done";
  return (
    <li className="flex gap-3 py-3" data-testid="sync-step" data-status={status}>
      <span className="mt-0.5 shrink-0" aria-hidden>
        {PHASE_ICON[status]}
      </span>
      <div className="flex min-w-0 flex-col">
        <span className={cn("text-sm font-medium", failed && "text-destructive")}>
          {failed ? "Sync from GitHub - failed" : "Sync from GitHub"}
        </span>
        <span className={cn("text-xs break-words", failed ? "text-destructive" : "text-muted-foreground")}>{text}</span>
      </div>
    </li>
  );
}

/** The last 64 KiB of the run's log, polled while the run is active. */
function RunLog({
  slug,
  runId,
  active,
  finished,
  open,
  onToggle,
}: {
  slug: string;
  runId: number;
  active: boolean;
  finished: ScanRunStatus | null;
  open: boolean;
  onToggle: () => void;
}) {
  const log = useQuery({
    // `finished` is part of the key so the log is read once more after the run ends.
    queryKey: projectKey(slug, "scan-log", runId, finished),
    queryFn: () => projectApi(slug).scanLog(runId),
    enabled: open,
    refetchInterval: active ? 2000 : false,
    retry: false,
  });
  const pre = useRef<HTMLPreElement>(null);
  const text = log.data?.text;
  useEffect(() => {
    if (pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [text]);

  return (
    <section className="rounded-xl border border-border bg-card shadow-card" data-testid="run-log">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="flex w-full items-center justify-between px-5 py-3 text-left text-sm font-semibold"
      >
        Log
        <span className="text-xs font-normal text-muted-foreground">{open ? "Hide" : "Show"}</span>
      </button>
      {open && (
        <div className="flex flex-col gap-2 border-t border-border px-5 py-3">
          {log.isLoading && <p className="text-xs text-muted-foreground">Loading the log…</p>}
          {log.isError && <ErrorState error={log.error} size="inline" onRetry={() => void log.refetch()} />}
          {log.data && (
            <>
              {log.data.truncated && (
                <p className="text-xs text-muted-foreground">
                  Showing the end of the log ({formatNumber(Math.round(log.data.size / 1024))} KiB in
                  all). Keys are redacted.
                </p>
              )}
              <pre
                ref={pre}
                className="max-h-72 overflow-auto rounded-md bg-muted p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all"
              >
                {log.data.text || "No output yet."}
              </pre>
            </>
          )}
        </div>
      )}
    </section>
  );
}

/** The cost lines of a run, on every outcome that has them (and only when the payload carries them). */
function CostLines({ runId, summary, canSeeCalls }: { runId: number; summary: ScanRunRow["summary"]; canSeeCalls: boolean }) {
  const usage = usageLine(summary);
  const estimate = summary?.estimate?.cost_usd;
  if (!usage && typeof estimate !== "number") return null;
  return (
    <>
      {usage && (
        <p data-testid="run-usage">
          {usage}
          {canSeeCalls && (
            <>
              {" "}
              <Link
                to="/usage"
                search={{ tab: "calls", scan_run: String(runId) }}
                className="text-primary-text hover:underline"
              >
                View these calls
              </Link>
            </>
          )}
        </p>
      )}
      {typeof estimate === "number" && (
        <p data-testid="run-estimate">Estimated {formatUsd(estimate)} before the run</p>
      )}
    </>
  );
}

function Outcome({
  slug,
  runId,
  state,
  row,
  viewerUid,
  canSeeCalls,
  retry,
  onOpenLog,
}: {
  slug: string;
  runId: number;
  state: ScanRunState;
  row: ScanRunRow;
  viewerUid: string | null;
  canSeeCalls: boolean;
  /** Retry the run's own kind of scan; `null` when this viewer may not (or it is not offered). */
  retry: { run: () => void; pending: boolean; allowed: boolean; reason?: string } | null;
  onOpenLog: () => void;
}) {
  const status = state.finished;
  if (!status) return null;
  const summary = state.summary ?? row.summary ?? null;
  const elapsed = summary?.elapsed_sec;
  const costs = <CostLines runId={runId} summary={summary} canSeeCalls={canSeeCalls} />;

  if (typeof summary?.merged_into === "number") {
    return (
      <Alert data-testid="run-result">
        <AlertTitle>Covered by another run</AlertTitle>
        <AlertDescription>
          A request that arrived while a run was waiting was merged into{" "}
          <Link
            to="/p/$slug/scans/{-$runId}"
            params={{ slug, runId: String(summary.merged_into) }}
            className="text-primary-text hover:underline"
          >
            that run
          </Link>
          .
        </AlertDescription>
      </Alert>
    );
  }
  if (status === "ok") {
    const merged = { ...summary, moved: summary?.moved ?? state.sync?.moved, cloned: summary?.cloned ?? state.sync?.cloned };
    const synced = row.kind === "sync" ? syncOutcome(row, merged) : null;
    return (
      <Alert data-testid="run-result">
        <CheckCircle2Icon />
        <AlertTitle>
          {synced ?? `Finished${typeof elapsed === "number" ? ` in ${formatSeconds(elapsed)}` : ""}`}
        </AlertTitle>
        <AlertDescription>
          {synced && typeof elapsed === "number" && <p>Finished in {formatSeconds(elapsed)}.</p>}
          {summary?.analyze_skipped === "budget" ? (
            <p>{BUDGET_SKIPPED}</p>
          ) : summary?.analyze_skipped === "--codegraph-only" ? (
            <p>{CODE_INDEX_REFRESHED}. The history is on the platform.</p>
          ) : summary?.analyze_skipped && summary.analyze_skipped !== "--skip-analyze" ? (
            <p>Commit descriptions were skipped: {summary.analyze_skipped}</p>
          ) : summary?.analyze_skipped || !row.analyze ? (
            <p>Structure only: git history and code structure, no LLM calls.</p>
          ) : (
            <p>Git history, code structure and commit descriptions are up to date.</p>
          )}
          {costs}
        </AlertDescription>
      </Alert>
    );
  }
  if (status === "failed") {
    const failure = failureSummary(summary, state.error);
    const syncFailed = state.sync?.status === "failed";
    return (
      <Alert variant="destructive" data-testid="run-result">
        <XCircleIcon />
        <AlertTitle>{syncFailed ? "The sync from GitHub failed" : "The scan failed"}</AlertTitle>
        <AlertDescription>
          <p>{failure.message}</p>
          {costs}
          <div className="mt-2 flex flex-wrap gap-2">
            {retry &&
              (retry.allowed ? (
                <Button size="sm" variant="outline" onClick={retry.run} disabled={retry.pending} data-testid="run-retry">
                  Retry
                </Button>
              ) : (
                <span className="self-center text-xs text-muted-foreground" data-testid="run-retry-reason">
                  {retry.reason}
                </span>
              ))}
            {failure.settings && (
              <Button
                size="sm"
                variant="outline"
                render={<Link to="/p/$slug/settings" params={{ slug }} search={{ section: failure.settings }} />}
              >
                Open settings
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={onOpenLog} data-testid="run-open-log">
              Open the log
            </Button>
          </div>
          {failure.details.length > 0 && (
            <details className="mt-2 text-xs" data-testid="run-error-details">
              <summary className="cursor-pointer select-none">Show details</summary>
              <div className="mt-1 flex flex-col gap-1 break-words whitespace-pre-wrap">
                {failure.details.map((d, i) => (
                  <p key={i}>{d}</p>
                ))}
              </div>
            </details>
          )}
        </AlertDescription>
      </Alert>
    );
  }
  if (status === "interrupted") {
    return (
      <Alert data-testid="run-result">
        <AlertTitle>Interrupted</AlertTitle>
        <AlertDescription>
          The portal stopped while this ran. Nothing is lost; run another scan to finish the job.
          {costs}
        </AlertDescription>
      </Alert>
    );
  }
  const budget = summary?.cancelled_by === "budget";
  return (
    <Alert data-testid="run-result">
      <AlertTitle>{cancelledLabel({ cancelled_by: row.cancelled_by, summary }, viewerUid)}</AlertTitle>
      <AlertDescription>
        {budget ? (
          <p>
            The monthly budget ran out, so the run was stopped. Commits it had already described are kept; run
            another scan once the budget is raised or next month.
          </p>
        ) : row.analyze ? (
          <p>Commits it had already described are kept; run another scan to finish the job.</p>
        ) : (
          <p>The project keeps the data from its last finished scan.</p>
        )}
        {costs}
      </AlertDescription>
    </Alert>
  );
}

/**
 * One scan run, live. The header comes from `GET /scans/<id>` (cached under
 * `scanRunKey`, which the breadcrumb reads): the run's title, status and a meta
 * line. Phases come from the events stream, every one named up front from the
 * `start` event, with a bar per task that reports a total (the LLM descriptions
 * phase) and timings once the run's `result` arrives; then the outcome and the
 * tail of `runs/<id>.log`. The stream is followed to its `end` frame, and
 * reconnects on its own, so a reload or a second viewer sees the same run. A run
 * that does not exist renders "not found" and requests nothing more. "Rescan"
 * here coalesces: a click during a run queues one follow-up and every further
 * click reports the same run id.
 */
export function ScanRunView({ slug, runId }: { slug: string; runId: number }) {
  const queryClient = useQueryClient();
  const run = useQuery({
    queryKey: scanRunKey(slug, runId),
    queryFn: () => projectApi(slug).scan(runId),
    retry: false,
    refetchInterval: (q) => (q.state.data?.status === "queued" || q.state.data?.status === "running" ? 3000 : false),
  });
  const row = run.data ?? null;
  const missing = run.error instanceof ApiError && run.error.status === 404;
  // A run the API says does not exist is not followed: no events request, no polling.
  const live = useScanRun(slug, missing ? null : runId);
  const status: ScanRunStatus | null = live.finished ?? row?.status ?? null;
  const active = status === "queued" || status === "running" || (status === null && !live.failure);
  const { scanNow, scanPending, followUp } = useScanActions(slug, {
    id: runId,
    active,
  });
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });
  const portal = usePortalState().data;
  const viewerUid = portal?.user?.uid ?? null;
  const [logChoice, setLogChoice] = useState<boolean | null>(null);
  // A contributor may stop a structure-only run; a run that may spend needs the full scan action.
  const mayCancel =
    can(project.data, "project.scan_full") || (can(project.data, "project.scan") && row?.analyze === false);
  const now = useNow(1000, status === "running");
  // The follow-up notice holds only while this run is active and the follow-up is
  // still waiting behind it (BUG-4); a row not listed yet counts as queued.
  const followUpRun = useQuery({
    // Under the `scans` prefix, so a rescan request (which invalidates it) refreshes the follow-up too.
    queryKey: [...projectKey(slug, "scans"), "follow-up", followUp],
    queryFn: () => projectApi(slug).scan(followUp as number),
    enabled: followUp !== null && followUp !== runId && active,
    retry: false,
    refetchInterval: 3000,
  });
  const showFollowUp =
    active &&
    followUp !== null &&
    followUp !== runId &&
    (followUpRun.data === undefined || followUpRun.data.status === "queued");

  // The cached row is stale the moment the stream ends: refresh it, the history and
  // the project card's badge once.
  useEffect(() => {
    if (live.finished) {
      void queryClient.invalidateQueries({ queryKey: scanRunKey(slug, runId) });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "scans") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    }
  }, [live.finished, queryClient, slug, runId]);

  if (missing || live.failure?.status === 404) {
    return <NotFoundState kind="run" back={{ label: "Back to scans", to: `/p/${slug}/scans` }} />;
  }
  if (live.failure) {
    const problem = projectProblem(new ApiError(live.failure.status, live.failure.message, live.failure.code));
    return (
      <PageContainer width="default">
        <Alert variant="destructive" data-testid="run-unavailable">
          <AlertTitle>This run cannot be shown</AlertTitle>
          <AlertDescription>
            {live.failure.message}
            {live.failure.code === "unsafe_path" && <p className="mt-1">{problem.message}</p>}
          </AlertDescription>
        </Alert>
        <div className="mt-3">
          <Button variant="outline" render={<Link to="/p/$slug/scans/{-$runId}" params={{ slug }} />}>
            Back to scans
          </Button>
        </div>
      </PageContainer>
    );
  }
  if (run.isError) {
    return (
      <PageContainer width="default">
        <ErrorState error={run.error} title="Couldn't load this run" onRetry={() => void run.refetch()} size="page" />
      </PageContainer>
    );
  }
  if (!row) {
    return (
      <PageContainer width="default">
        <DetailSkeleton label="Loading the run" />
      </PageContainer>
    );
  }

  const phases = phaseRows(live);
  const cg = codegraphRow(live);
  const duration =
    status === "running" && row.started_at
      ? Math.max(0, (now - Date.parse(row.started_at)) / 1000)
      : runSeconds({ ...row, summary: live.summary ?? row.summary });
  const waiting = status === "queued" || (status === null && live.phaseTotal === null);
  const badStatus = status === "failed" || status === "interrupted";
  const logOpen = logChoice ?? badStatus;
  const phaseNow = phases.find((p) => p.status === "running");
  const availability = project.data ? scanAvailability(project.data) : null;
  const retryKind = row.analyze ? availability?.full : availability?.quick;
  const retry =
    project.data && retryKind && (project.data.permissions?.includes("project.scan") ?? false)
      ? {
          run: () => scanNow(row.analyze ? FULL_SCAN : QUICK_SCAN),
          pending: scanPending,
          allowed: retryKind.allowed,
          reason: retryKind.reason,
        }
      : null;
  const meta = [
    `Requested by ${requesterLabel(row, viewerUid).replace(/^You$/, "you")}`,
    row.queued_at ? `queued ${formatClock(row.queued_at)}` : null,
    row.started_at ? `started ${formatClock(row.started_at)}` : null,
    duration !== null ? formatSeconds(duration) : null,
  ].filter(Boolean);
  const openLog = () => {
    setLogChoice(true);
    setTimeout(() => document.querySelector('[data-testid="run-log"]')?.scrollIntoView?.({ block: "nearest" }), 0);
  };

  return (
    <PageContainer width="default" className="flex flex-col gap-4" >
      <div className="flex flex-wrap items-start justify-between gap-3" data-testid="run-view">
        <div className="flex min-w-0 flex-col gap-1">
          <h1 className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[22px] font-semibold tracking-tight">
            {runTitle(row)}
            {status && <RunStatusBadge status={status} />}
          </h1>
          <p className="text-[13px] text-muted-foreground" data-testid="run-meta">
            {meta.join(" · ")}
          </p>
        </div>
        <div className="flex gap-2">
          {mayCancel && (status === "queued" || status === "running") && (
            <CancelRunButton slug={slug} runId={runId} status={status} kind={row.kind} analyze={row.analyze} />
          )}
          {project.data && (
            <ScanMenu
              project={project.data}
              variant={active ? "outline" : "default"}
              onScan={scanNow}
              disabled={scanPending}
            />
          )}
        </div>
      </div>

      {showFollowUp && (
        <Alert data-testid="followup">
          <AlertTitle>Another request joined this run</AlertTitle>
          <AlertDescription>
            A follow-up scan starts when this one finishes, and asking again adds nothing more.{" "}
            <Link
              to="/p/$slug/scans/{-$runId}"
              params={{ slug, runId: String(followUp) }}
              className="text-primary-text hover:underline"
            >
              Open the follow-up
            </Link>
            .
          </AlertDescription>
        </Alert>
      )}

      {live.error && status !== "failed" && (
        <Alert variant="destructive" data-testid="run-error">
          <AlertTitle>The scan could not run</AlertTitle>
          <AlertDescription>{live.error}</AlertDescription>
        </Alert>
      )}

      <Outcome
        slug={slug}
        runId={runId}
        state={live}
        row={row}
        viewerUid={viewerUid}
        canSeeCalls={!!portal?.usage?.org}
        retry={retry}
        onOpenLog={openLog}
      />

      <section className="rounded-xl border border-border bg-card px-5 py-1 shadow-card" aria-label="Phases">
        {waiting ? (
          <p className="py-4 text-sm text-muted-foreground" data-testid="run-waiting">
            <CircleDashedIcon className="mr-2 inline size-4 align-text-bottom" />
            {status === "queued"
              ? "Waiting for its turn. Another scan is running for this project, or the portal is at its limit of concurrent scans."
              : "Starting…"}
          </p>
        ) : (
          <>
            {phaseNow && phases.length > 1 && (
              <p className="pt-3 text-xs text-muted-foreground" data-testid="phase-count">
                Phase {phaseNow.phase} of {phases.length}
              </p>
            )}
            <ul className="flex flex-col divide-y divide-border">
              {live.sync && <SyncItem sync={live.sync} />}
              {phases.map((p) => (
                <PhaseItem key={p.phase} phase={p} />
              ))}
              {(cg.task || cg.result) && (
                <li className="flex gap-3 py-3" data-testid="codegraph-step">
                  <span className="mt-0.5 shrink-0" aria-hidden>
                    {
                      PHASE_ICON[
                        cg.result
                          ? cg.result.status === "failed"
                            ? "failed"
                            : "done"
                          : live.finished === "cancelled"
                            ? "cancelled"
                            : live.finished
                              ? "skipped"
                              : "running"
                      ]
                    }
                  </span>
                  <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <span className="text-sm font-medium">CodeGraph index</span>
                    <span className="text-xs text-muted-foreground">
                      Runs alongside the phases.{" "}
                      {cg.result ? (cg.result.error ?? cg.result.summary ?? "") : (cg.task?.description ?? "")}
                    </span>
                  </div>
                </li>
              )}
            </ul>
          </>
        )}
      </section>

      <RunLog
        slug={slug}
        runId={runId}
        active={active}
        finished={live.finished}
        open={logOpen}
        onToggle={() => setLogChoice(!logOpen)}
      />
    </PageContainer>
  );
}
