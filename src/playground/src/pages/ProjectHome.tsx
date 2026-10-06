import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { CloudIcon, FolderGit2Icon, GitBranchIcon, LockIcon } from "lucide-react";
import { portalApi, projectApi, projectKey } from "../api";
import { projectProblem } from "../lib/errors";
import { useSlug } from "../lib/project";
import { projectRoleLabel } from "../lib/platformLink";
import { timeAgo } from "../lib/projectStatus";
import { can } from "../lib/permissions";
import { useScanActions } from "../lib/scanActions";
import { formatSeconds, runSeconds, triggerLabel } from "../lib/scanFormat";
import { AccessLostNotice, UnsupportedSourceNotice } from "../components/portal/AccessLost";
import { LinkNotice } from "../components/portal/LinkNotice";
import { PlatformButtons } from "../components/portal/LinkedActions";
import { UseWithAgent } from "../components/portal/UseWithAgent";
import { ConnectAgent } from "../components/portal/ConnectAgent";
import { NotInitialized, ProblemAlert, ProjectUnavailable } from "../components/portal/EdgeStates";
import { ProjectPortChangeNotice } from "../components/portal/PortChangeNotice";
import { ProjectStatusBadge } from "../components/portal/ProjectStatusBadge";
import { RunStatusBadge } from "../components/portal/RunStatusBadge";
import { ScanMenu } from "../components/portal/ScanMenu";
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
 * *Scan now*), stats, the describe-cost card when many commits wait,
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
    // A linked project has no describe cost: the route is refused (M2e).
    enabled: ready && !!p?.last_scan_at && p?.source !== "platform",
    retry: false,
  });
  const { scanNow, scanPending } = useScanActions(slug);
  const readOnly = useReadOnly();
  const portal = usePortalState().data;
  const production = isProduction(portal);
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
  const linked = p.source === "platform";
  // Access lost / an unsupported source: what was scanned stays readable, scans are refused.
  const scannable = ready && !p.access_lost && p.source_supported !== false;

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-5 p-6 sm:p-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1.5">
          <h1 className="text-[22px] font-semibold tracking-tight">{p.name}</h1>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <Badge variant="outline" className="gap-1">
              {github ? <GitBranchIcon /> : linked ? <CloudIcon /> : <FolderGit2Icon />}
              {github ? "GitHub" : linked ? "Platform" : "Local"}
            </Badge>
            {p.restricted && (
              <Badge variant="outline" className="gap-1" data-testid="restricted-badge">
                <LockIcon />
                Restricted
              </Badge>
            )}
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
          {ready && !linked && (
            <Button variant="outline" render={<Link to="/p/$slug/explorer" params={{ slug }} />}>
              Open Explorer
            </Button>
          )}
          <ScanMenu project={p} onScan={scanNow} disabled={!scannable || scanPending} />
        </div>
      </div>

      <AccessLostNotice project={p} />
      <UnsupportedSourceNotice project={p} />
      {linked && (
        <section
          data-testid="linked-panel"
          className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5"
        >
          <h2 className="text-sm font-semibold">Linked project</h2>
          <LinkNotice project={p} />
          {projectRoleLabel(p.link) && (
            <p data-testid="linked-role" className="text-xs text-muted-foreground">
              Your role on the platform project: {projectRoleLabel(p.link)}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            The history, Explorer and Chat for this project are on the platform. This machine indexes
            your checkout so your agent can tell its own changes from the platform's history.
          </p>
          <PlatformButtons project={p} />
        </section>
      )}
      {p.root_status !== "ok" && <ProjectUnavailable project={p} />}
      <ProjectPortChangeNotice slug={slug} change={p.port_change} />
      {p.root_status === "ok" && !p.initialized && <NotInitialized slug={slug} />}
      {problem?.kind === "unsafe_path" && <ProblemAlert problem={problem} />}

      {ready && stale && (can(p, "project.scan") || can(p, "project.scan_full")) && (
        <Alert data-testid="stale-banner">
          <AlertTitle>
            {stale.commits_behind === null
              ? "The repository history changed since the last scan"
              : `${stale.commits_behind} commit${stale.commits_behind === 1 ? "" : "s"} behind`}
          </AlertTitle>
          <AlertDescription>
            <p>
              {linked
                ? "The checkout is ahead of the code index on this machine, so your agent places the newest work less precisely."
                : "The checkout is ahead of what WhyGraph last scanned, so the Explorer and Chat miss the newest work."}
            </p>
            <div className="mt-2 flex gap-2">
              <ScanMenu project={p} size="sm" onScan={scanNow} disabled={!scannable || scanPending} />
            </div>
          </AlertDescription>
        </Alert>
      )}

      {ready && !linked && (
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
      {ready && !linked && !stats && p.last_scan_at && (
        <p className="text-xs text-muted-foreground">Stats are unavailable right now.</p>
      )}

      {ready && can(p, "project.scan_full") && !dismissed && estimate.data && estimate.data.commits > ESTIMATE_THRESHOLD && (
        <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5">
          <h2 className="text-sm font-semibold">Commits waiting for a description</h2>
          <EstimateBody
            slug={slug}
            estimate={estimate.data}
            canDescribe={can(p, "project.scan_full")}
            canConfigure={can(p, "project.configure")}
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

      {production && ready && !readOnly && portal?.org && portal.base_url && (
        <UseWithAgent baseUrl={portal.base_url} org={portal.org.slug} slug={slug} />
      )}
      {p.initialized && p.mcp_url && !production && <ConnectAgent mcpUrl={p.mcp_url} configured={p.agents} />}
    </div>
  );
}
