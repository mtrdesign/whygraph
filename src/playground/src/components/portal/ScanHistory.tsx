import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { HistoryIcon } from "lucide-react";
import { portalApi, projectApi, projectKey } from "../../api";
import { useReadOnly } from "../../lib/identity";
import { projectProblem } from "../../lib/errors";
import { timeAgo } from "../../lib/projectStatus";
import { useScanActions } from "../../lib/scanActions";
import { formatSeconds, runOutcome, runSeconds, triggerLabel } from "../../lib/scanFormat";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "../ui/empty";
import { Skeleton } from "../ui/skeleton";
import { RunStatusBadge } from "./RunStatusBadge";

/**
 * Screen 8: the project's recent runs (the newest 50) with trigger, status and
 * duration; each row opens the run. Refreshes itself while a run is active.
 */
export function ScanHistory({ slug }: { slug: string }) {
  const runs = useQuery({
    queryKey: projectKey(slug, "scans"),
    queryFn: () => projectApi(slug).scans(),
    retry: false,
    refetchInterval: (q) =>
      q.state.data?.runs.some((r) => r.status === "queued" || r.status === "running") ? 3000 : false,
  });
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });
  const { scanNow, scanPending } = useScanActions(slug);
  const readOnly = useReadOnly();
  const list = runs.data?.runs ?? [];
  const usable = !!project.data?.initialized && project.data.root_status === "ok";

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-5 p-6 sm:p-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">Scans</h1>
          <p className="text-[13px] text-muted-foreground">
            Every scan and sync for this project, newest first.
          </p>
        </div>
        {!readOnly && (
          <div className="flex gap-2">
            <Button onClick={() => scanNow()} disabled={!usable || scanPending}>
              Scan now
            </Button>
          </div>
        )}
      </div>

      {runs.isLoading && <Skeleton className="h-40 rounded-xl" />}
      {runs.isError && (
        <Alert variant="destructive" data-testid="history-error">
          <AlertTitle>{projectProblem(runs.error).title}</AlertTitle>
          <AlertDescription>{projectProblem(runs.error).message}</AlertDescription>
        </Alert>
      )}
      {runs.isSuccess && list.length === 0 && (
        <Empty className="border border-dashed border-border py-14">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <HistoryIcon />
            </EmptyMedia>
            <EmptyTitle>No scans yet</EmptyTitle>
            <EmptyDescription>Scan now to index this project's history and code structure.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
      {list.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-sm" data-testid="scan-history">
            <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">Run</th>
                <th className="px-4 py-2 font-medium">Trigger</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Started</th>
                <th className="px-4 py-2 font-medium">Duration</th>
                <th className="px-4 py-2 font-medium">Result</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {list.map((run) => {
                const seconds = runSeconds(run);
                const outcome = runOutcome(run);
                return (
                  <tr key={run.id} data-testid={`run-${run.id}`} className="hover:bg-muted/30">
                    <td className="px-4 py-2.5">
                      <Link
                        to="/p/$slug/scans/{-$runId}"
                        params={{ slug, runId: String(run.id) }}
                        className="font-mono text-primary-text hover:underline"
                      >
                        #{run.id}
                      </Link>
                    </td>
                    <td className="px-4 py-2.5">{triggerLabel(run)}</td>
                    <td className="px-4 py-2.5">
                      <RunStatusBadge status={run.status} />
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {timeAgo(run.started_at) ?? (run.status === "queued" ? "waiting" : "-")}
                    </td>
                    <td className="px-4 py-2.5 tabular-nums text-muted-foreground">
                      {seconds !== null ? formatSeconds(seconds) : "-"}
                    </td>
                    <td className="max-w-64 truncate px-4 py-2.5 text-xs text-muted-foreground" title={outcome ?? ""}>
                      {outcome ?? ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
