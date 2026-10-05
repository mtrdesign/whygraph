import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2Icon,
  CircleDashedIcon,
  CircleIcon,
  MinusCircleIcon,
  XCircleIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { ApiError, projectApi, projectKey, type ScanRunRow, type ScanRunStatus } from "../../api";
import { useReadOnly } from "../../lib/identity";
import { projectProblem } from "../../lib/errors";
import { useScanActions } from "../../lib/scanActions";
import { formatSeconds, runSeconds, triggerLabel } from "../../lib/scanFormat";
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
import { timeAgo } from "../../lib/projectStatus";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Progress } from "../ui/progress";
import { Spinner } from "../ui/spinner";
import { CancelRunButton } from "./CancelRunButton";
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
              {task.completed.toLocaleString("en-US")} / {task.total.toLocaleString("en-US")}
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
          <span className={cn("text-sm font-medium", phase.status === "pending" && "text-muted-foreground")}>
            {phase.title}
          </span>
          {phase.seconds !== null && (
            <span className="text-xs tabular-nums text-muted-foreground">{formatSeconds(phase.seconds)}</span>
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
  const text =
    sync.status === "fetching"
      ? "Fetching from GitHub"
      : sync.status === "failed"
        ? `Fetch failed${sync.error ? `: ${sync.error}` : ""}`
        : sync.moved
          ? "Fetched new commits; scanning them next"
          : "Already up to date; nothing to scan";
  const status: PhaseStatus =
    sync.status === "fetching" ? "running" : sync.status === "failed" ? "failed" : "done";
  return (
    <li className="flex gap-3 py-3" data-testid="sync-step" data-status={status}>
      <span className="mt-0.5 shrink-0" aria-hidden>
        {PHASE_ICON[status]}
      </span>
      <div className="flex flex-col">
        <span className="text-sm font-medium">Sync with GitHub</span>
        <span className={cn("text-xs", status === "failed" ? "text-destructive" : "text-muted-foreground")}>
          {text}
        </span>
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
  autoOpen,
}: {
  slug: string;
  runId: number;
  active: boolean;
  finished: ScanRunStatus | null;
  autoOpen: boolean;
}) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? autoOpen;
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
    <section className="rounded-xl border border-border bg-card" data-testid="run-log">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setUserOpen(!open)}
        className="flex w-full items-center justify-between px-5 py-3 text-left text-sm font-semibold"
      >
        Log
        <span className="text-xs font-normal text-muted-foreground">{open ? "Hide" : "Show"}</span>
      </button>
      {open && (
        <div className="flex flex-col gap-2 border-t border-border px-5 py-3">
          {log.isLoading && <p className="text-xs text-muted-foreground">Loading the log…</p>}
          {log.isError && <p className="text-xs text-destructive">Could not read the log: {log.error.message}</p>}
          {log.data && (
            <>
              {log.data.truncated && (
                <p className="text-xs text-muted-foreground">
                  Showing the end of the log ({Math.round(log.data.size / 1024).toLocaleString("en-US")} KiB in
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

function Outcome({ slug, state, row }: { slug: string; state: ScanRunState; row: ScanRunRow | null }) {
  const status = state.finished;
  if (!status) return null;
  const summary = state.summary ?? row?.summary ?? null;
  const elapsed = summary?.elapsed_sec;

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
            run #{summary.merged_into}
          </Link>
          .
        </AlertDescription>
      </Alert>
    );
  }
  if (status === "ok") {
    return (
      <Alert data-testid="run-result">
        <CheckCircle2Icon />
        <AlertTitle>
          {state.sync?.status === "ok" && state.sync.moved === false
            ? "Already up to date"
            : `Finished${typeof elapsed === "number" ? ` in ${formatSeconds(elapsed)}` : ""}`}
        </AlertTitle>
        <AlertDescription>
          {summary?.analyze_skipped && summary.analyze_skipped !== "--skip-analyze" ? (
            <p>Commit descriptions were skipped: {summary.analyze_skipped}</p>
          ) : summary?.analyze_skipped || (row && !row.analyze) ? (
            <p>Structure only: git history and code structure, no LLM calls.</p>
          ) : (
            <p>Git history, code structure and commit descriptions are up to date.</p>
          )}
        </AlertDescription>
      </Alert>
    );
  }
  if (status === "failed") {
    const failed = (state.result?.crawlers ?? summary?.crawlers ?? []).filter((c) => c.status === "failed");
    const message =
      state.error ??
      (typeof summary?.error === "string" ? summary.error : null) ??
      (failed.length ? null : `The scan exited with code ${summary?.exit_code ?? "?"}.`);
    return (
      <Alert variant="destructive" data-testid="run-result">
        <XCircleIcon />
        <AlertTitle>The scan failed</AlertTitle>
        <AlertDescription>
          {message && <p>{message}</p>}
          {failed.map((c) => (
            <p key={c.name}>
              <span className="font-medium">{crawlerLabel(c.name)}:</span> {c.error ?? "failed"}
            </p>
          ))}
          <p className="mt-1">The log below has the detail.</p>
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
        </AlertDescription>
      </Alert>
    );
  }
  return (
    <Alert data-testid="run-result">
      <AlertTitle>Cancelled</AlertTitle>
      {summary?.cancelled_by === "user" && (
        <AlertDescription>
          You cancelled this run. Commits it had already described are kept; run another scan to finish the
          job.
        </AlertDescription>
      )}
    </Alert>
  );
}

/**
 * Screen 7: one scan run, live. Phases come from the events stream (their count
 * from the first event's `phase_total`) with a bar per task that reports a total
 * (the LLM descriptions phase), timings once the run's `result` arrives, then the
 * outcome and the tail of `runs/<id>.log`. The stream is followed to its `end`
 * frame, and reconnects on its own, so a reload or a second viewer sees the same
 * run. "Scan now" here coalesces: a click during a run queues one follow-up and
 * every further click reports the same run id.
 */
export function ScanRunView({ slug, runId }: { slug: string; runId: number }) {
  const queryClient = useQueryClient();
  const runs = useQuery({
    queryKey: projectKey(slug, "scans"),
    queryFn: () => projectApi(slug).scans(),
    refetchInterval: (q) =>
      q.state.data?.runs.some((r) => r.id === runId && (r.status === "queued" || r.status === "running"))
        ? 3000
        : false,
  });
  const row = runs.data?.runs.find((r) => r.id === runId) ?? null;
  const live = useScanRun(slug, runId);
  const status: ScanRunStatus | null = live.finished ?? row?.status ?? null;
  const active = status === "queued" || status === "running" || (status === null && !live.failure);
  const { scanNow, scanPending, followUp } = useScanActions(slug, {
    id: runId,
    active,
  });
  const readOnly = useReadOnly();
  const now = useNow(1000, status === "running");

  // The list row is stale the moment the stream ends: refresh it (and the project
  // card's badge) once.
  useEffect(() => {
    if (live.finished) {
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "scans") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    }
  }, [live.finished, queryClient, slug]);

  if (live.failure) {
    const problem = projectProblem(
      new ApiError(live.failure.status, live.failure.message, live.failure.code),
    );
    const notFound = live.failure.status === 404;
    return (
      <div className="mx-auto w-full max-w-3xl p-6">
        <Alert variant="destructive" data-testid="run-unavailable">
          <AlertTitle>{notFound ? `Run #${runId} was not found` : "This run cannot be shown"}</AlertTitle>
          <AlertDescription>
            {notFound ? "It may belong to another project." : live.failure.message}
            {live.failure.code === "unsafe_path" && <p className="mt-1">{problem.message}</p>}
          </AlertDescription>
        </Alert>
        <div className="mt-3">
          <Button variant="outline" render={<Link to="/p/$slug/scans/{-$runId}" params={{ slug }} />}>
            Back to scans
          </Button>
        </div>
      </div>
    );
  }

  const phases = phaseRows(live);
  const cg = codegraphRow(live);
  const kind = row?.kind ?? (live.sync ? "sync" : "scan");
  const started = row?.started_at ?? null;
  const duration =
    status === "running" && started
      ? Math.max(0, (now - Date.parse(started)) / 1000)
      : row
        ? runSeconds({ ...row, summary: live.summary ?? row.summary })
        : null;
  const waiting = status === "queued" || (status === null && live.phaseTotal === null);
  const badStatus = status === "failed" || status === "interrupted";

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6 sm:p-8" data-testid="run-view">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="flex items-center gap-3 text-[22px] font-semibold tracking-tight">
            {kind === "sync" ? "Sync" : "Scan"} #{runId}
            {status && <RunStatusBadge status={status} />}
          </h1>
          <p className="text-[13px] text-muted-foreground">
            {row ? triggerLabel(row) : "Loading"}
            {row?.analyze === false && kind === "scan" ? " · structure only" : ""}
            {started ? ` · started ${timeAgo(started)}` : ""}
            {duration !== null ? ` · ${formatSeconds(duration)}` : ""}
          </p>
        </div>
        {!readOnly && (
          <div className="flex gap-2">
            {(status === "queued" || status === "running") && (
              <CancelRunButton slug={slug} runId={runId} status={status} kind={kind} />
            )}
            <Button variant={active ? "outline" : "default"} onClick={() => scanNow()} disabled={scanPending}>
              {active ? "Scan now" : "Scan again"}
            </Button>
          </div>
        )}
      </div>

      {followUp !== null && followUp !== runId && (
        <Alert data-testid="followup">
          <AlertTitle>Another scan is queued</AlertTitle>
          <AlertDescription>
            It starts when this one finishes:{" "}
            <Link
              to="/p/$slug/scans/{-$runId}"
              params={{ slug, runId: String(followUp) }}
              className="text-primary-text hover:underline"
            >
              run #{followUp}
            </Link>
            . Asking again folds into the same run.
          </AlertDescription>
        </Alert>
      )}

      {live.error && (
        <Alert variant="destructive" data-testid="run-error">
          <AlertTitle>The scan could not run</AlertTitle>
          <AlertDescription>{live.error}</AlertDescription>
        </Alert>
      )}

      <Outcome slug={slug} state={live} row={row} />

      <section className="rounded-xl border border-border bg-card px-5 py-1" aria-label="Phases">
        {waiting ? (
          <p className="py-4 text-sm text-muted-foreground" data-testid="run-waiting">
            <CircleDashedIcon className="mr-2 inline size-4 align-text-bottom" />
            {status === "queued"
              ? "Waiting for its turn. Another scan is running for this project, or the portal is at its limit of concurrent scans."
              : "Starting…"}
          </p>
        ) : (
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
        )}
      </section>

      <RunLog slug={slug} runId={runId} active={active} finished={live.finished} autoOpen={badStatus} />
    </div>
  );
}
