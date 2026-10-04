import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { FolderGit2Icon, GitBranchIcon } from "lucide-react";
import { portalApi, projectApi, projectKey } from "../api";
import { projectProblem } from "../lib/errors";
import { useSlug } from "../lib/project";
import { timeAgo } from "../lib/projectStatus";
import { useScanActions } from "../lib/scanActions";
import { formatSeconds, runSeconds, triggerLabel } from "../lib/scanFormat";
import { ConnectAgent } from "../components/portal/ConnectAgent";
import { NotInitialized, ProblemAlert, ProjectUnavailable } from "../components/portal/EdgeStates";
import { ProjectPortChangeNotice } from "../components/portal/PortChangeNotice";
import { ProjectStatusBadge } from "../components/portal/ProjectStatusBadge";
import { RunStatusBadge } from "../components/portal/RunStatusBadge";
import { EstimateBody } from "../components/portal/ScanEstimateCard";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { useReadOnly, usePortalState, isProduction } from "../lib/identity";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";

/** Waiting commits above which the cost card is shown on the overview (§4.14). */
const ESTIMATE_THRESHOLD = 500;

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border border-border bg-card p-4" data-testid={`stat-${label}`}>
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-2xl font-semibold tabular-nums tracking-tight">{value}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </div>
  );
}

/**
 * Screen 9a, the project's landing page: status (with the stale badge and its
 * *Scan now* / *Sync now*), stats, the describe-cost card when many commits wait,
 * recent scans, and how to connect an agent. Edge states replace or precede the
 * content: folder missing, not initialized, a symlink in the way.
 */
export function ProjectHome() {
  const slug = useSlug();
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
    refetchInterval: (q) => (q.state.data?.running_scan ? 3000 : false),
  });
  const p = project.data;
  const ready = !!p && p.initialized && p.root_status === "ok";
  const runs = useQuery({
    queryKey: projectKey(slug, "scans"),
    queryFn: () => projectApi(slug).scans(),
    enabled: ready,
    retry: false,
  });
  const estimate = useQuery({
    queryKey: projectKey(slug, "scan-estimate"),
    queryFn: () => projectApi(slug).scanEstimate(),
    enabled: ready && !!p?.last_scan_at,
    retry: false,
  });
  const { scanNow, syncNow, scanPending, syncPending } = useScanActions(slug);
  const readOnly = useReadOnly();
  const production = isProduction(usePortalState().data);
  const [dismissed, setDismissed] = useState(false);

  if (project.isLoading) {
    return (
      <div className="mx-auto w-full max-w-4xl p-6">
        <Skeleton className="h-32 rounded-xl" />
      </div>
    );
  }
  if (!p) return null;

  const recent = (runs.data?.runs ?? []).slice(0, 5);
  const problem = runs.isError ? projectProblem(runs.error) : null;
  const stats = p.stats;
  const stale = p.stale;
  const github = p.source === "github";

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-5 p-6 sm:p-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1.5">
          <h1 className="text-[22px] font-semibold tracking-tight">{p.name}</h1>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <Badge variant="outline" className="gap-1">
              {github ? <GitBranchIcon /> : <FolderGit2Icon />}
              {github ? "GitHub" : "Local"}
            </Badge>
            <span className="font-mono">{p.root}</span>
            {p.remote_url && (
              <>
                <span aria-hidden>·</span>
                <span className="font-mono">{p.remote_url.replace(/^https?:\/\//, "")}</span>
              </>
            )}
            <span aria-hidden>·</span>
            <ProjectStatusBadge project={p} />
            {p.last_scan_at && <span>scanned {timeAgo(p.last_scan_at)}</span>}
          </div>
        </div>
        <div className="flex gap-2">
          {ready && (
            <Button variant="outline" render={<Link to="/p/$slug/explorer" params={{ slug }} />}>
              Open Explorer
            </Button>
          )}
          {github && !readOnly && (
            <Button variant="outline" onClick={syncNow} disabled={!ready || syncPending}>
              Sync now
            </Button>
          )}
          {!readOnly && (
            <Button onClick={() => scanNow()} disabled={!ready || scanPending}>
              Scan now
            </Button>
          )}
        </div>
      </div>

      {p.root_status !== "ok" && <ProjectUnavailable project={p} />}
      <ProjectPortChangeNotice slug={slug} change={p.port_change} />
      {p.root_status === "ok" && !p.initialized && <NotInitialized slug={slug} />}
      {problem?.kind === "unsafe_path" && <ProblemAlert problem={problem} />}

      {ready && stale && !readOnly && (
        <Alert data-testid="stale-banner">
          <AlertTitle>
            {stale.commits_behind === null
              ? "The repository history changed since the last scan"
              : `${stale.commits_behind} commit${stale.commits_behind === 1 ? "" : "s"} behind`}
          </AlertTitle>
          <AlertDescription>
            <p>
              The checkout is ahead of what WhyGraph last scanned, so the Explorer and Chat miss the
              newest work.
            </p>
            <div className="mt-2 flex gap-2">
              {github && (
                <Button size="sm" variant="outline" onClick={syncNow} disabled={syncPending}>
                  Sync now
                </Button>
              )}
              <Button size="sm" onClick={() => scanNow()} disabled={scanPending}>
                Scan now
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}

      {ready && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="stats">
          <Stat
            label="Commits"
            value={stats ? stats.commits.toLocaleString("en-US") : "-"}
          />
          <Stat
            label="Described by LLM"
            value={stats ? `${Math.round(stats.described_pct)}%` : "-"}
            hint={stats ? `${stats.described.toLocaleString("en-US")} of ${stats.commits.toLocaleString("en-US")}` : undefined}
          />
          <Stat
            label="Pull requests · issues"
            value={stats ? `${stats.pull_requests.toLocaleString("en-US")} · ${stats.issues.toLocaleString("en-US")}` : "-"}
          />
          <Stat
            label="Rationale cards"
            value={stats ? stats.rationale_cards.toLocaleString("en-US") : "-"}
            hint="cached"
          />
        </div>
      )}
      {ready && !stats && p.last_scan_at && (
        <p className="text-xs text-muted-foreground">Stats are unavailable right now.</p>
      )}

      {ready && !readOnly && !dismissed && estimate.data && estimate.data.commits > ESTIMATE_THRESHOLD && (
        <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5">
          <h2 className="text-sm font-semibold">Commits waiting for a description</h2>
          <EstimateBody
            slug={slug}
            estimate={estimate.data}
            busy={scanPending}
            onDescribe={() => scanNow({ trigger: "describe" })}
            onLater={() => setDismissed(true)}
          />
        </section>
      )}

      {ready && (
        <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5" data-testid="recent-scans">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Recent scans</h2>
            <Link
              to="/p/$slug/scans/{-$runId}"
              params={{ slug }}
              className="text-xs text-primary-text hover:underline"
            >
              View all
            </Link>
          </div>
          {runs.isLoading && <Skeleton className="h-16" />}
          {runs.isSuccess && recent.length === 0 && (
            <p className="text-sm text-muted-foreground">No scans yet.</p>
          )}
          {recent.length > 0 && (
            <ul className="flex flex-col divide-y divide-border">
              {recent.map((run) => {
                const seconds = runSeconds(run);
                return (
                  <li key={run.id} className="flex items-center gap-3 py-2 text-sm">
                    <Link
                      to="/p/$slug/scans/{-$runId}"
                      params={{ slug, runId: String(run.id) }}
                      className="font-mono text-primary-text hover:underline"
                    >
                      #{run.id}
                    </Link>
                    <span>{triggerLabel(run)}</span>
                    <RunStatusBadge status={run.status} />
                    <span className="ml-auto text-xs text-muted-foreground">
                      {seconds !== null ? `${formatSeconds(seconds)} · ` : ""}
                      {timeAgo(run.started_at) ?? ""}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}

      {p.initialized && p.mcp_url && !production && <ConnectAgent mcpUrl={p.mcp_url} configured={p.agents} />}
    </div>
  );
}
