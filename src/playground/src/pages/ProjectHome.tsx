import { lazy, Suspense, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { CloudIcon, ExternalLinkIcon, FolderGit2Icon, GitBranchIcon, InfoIcon, LockIcon } from "lucide-react";
import {
  portalApi,
  projectApi,
  projectKey,
  type ProjectDetails,
  type ProjectOverview,
  type ScanRunRow,
} from "../api";
import { projectProblem } from "../lib/errors";
import { formatDate, formatNumber, formatPct, formatRelative, formatUsd, timeAgo } from "../lib/format";
import { isProduction, usePortalState, useReadOnly } from "../lib/identity";
import { projectRoleLabel as platformRoleLabel, platformHost, reconnectSearch, linkNotice } from "../lib/platformLink";
import { projectRoleLabel, sourceLabel } from "../lib/labels";
import { plural } from "../lib/plural";
import { projectHealth, showScannedAgo } from "../lib/projectHealth";
import { useSlug } from "../lib/project";
import { can } from "../lib/permissions";
import { agentChart, agentKindLabel, coverageChart, EVENT_LABEL, EVENT_TONE } from "../lib/overview";
import { TASKS } from "../lib/configForm";
import { useScanActions } from "../lib/scanActions";
import { formatSeconds, runLabel, runSeconds, runTitle } from "../lib/scanFormat";
import { markerColor, useChartColors } from "../components/charts/chartTheme";
import { SpendBar } from "../components/usage/parts";
import { PageContainer } from "../components/layout/PageContainer";
import { PathText, pathRepeatsName } from "../components/layout/PathText";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { ConnectAgent } from "../components/portal/ConnectAgent";
import { HealthPanel } from "../components/portal/HealthPanel";
import { PlatformButtons } from "../components/portal/LinkedActions";
import { LinkNotice } from "../components/portal/LinkNotice";
import { ProjectPortChangeNotice } from "../components/portal/PortChangeNotice";
import { ProjectStatusBadge } from "../components/portal/ProjectStatusBadge";
import { RunStatusBadge } from "../components/portal/RunStatusBadge";
import { EstimateBody } from "../components/portal/ScanEstimateCard";
import { ScanMenu } from "../components/portal/ScanMenu";
import { UseWithAgent } from "../components/portal/UseWithAgent";
import { ErrorState } from "../components/state/ErrorState";
import { PageSkeleton, TableSkeleton } from "../components/state/Skeletons";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "../components/ui/tooltip";
import { cn } from "../lib/utils";

// ECharts stays out of the main bundle: the chart is loaded with the card.
const ChartBlock = lazy(() => import("../components/charts/ChartBlock").then((m) => ({ default: m.ChartBlock })));

const CARD = "flex flex-col gap-3 rounded-xl bg-card p-5 shadow-card";

/** A number tile with a one-line definition, and a zero that says why. */
function Tile({
  label,
  value,
  hint,
  info,
  testId,
  className,
  children,
}: {
  label: string;
  value: string;
  hint?: ReactNode;
  info: string;
  testId: string;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <div
      className={cn("flex min-w-0 flex-col gap-1 rounded-xl bg-card p-4 shadow-card", className)}
      data-testid={`stat-${testId}`}
    >
      {/* Two label lines are reserved at lg, where five tiles share the row and a long
          label wraps, so every value sits on the same line; the icon follows the text. */}
      <span className="text-xs text-muted-foreground lg:min-h-8">
        {label}
        <Tooltip>
          <TooltipTrigger
            render={
              <button
                type="button"
                className="ml-1 inline-flex rounded-sm align-[-1px] text-muted-foreground hover:text-foreground"
              />
            }
            aria-label={`About ${label}`}
          >
            <InfoIcon className="size-3" aria-hidden />
          </TooltipTrigger>
          <TooltipContent>{info}</TooltipContent>
        </Tooltip>
      </span>
      <span className="text-2xl font-semibold tabular-nums tracking-tight">{value}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      {children}
    </div>
  );
}

function Tiles({ project, production }: { project: ProjectDetails; production: boolean }) {
  const stats = project.stats;
  const slug = project.slug;
  if (!stats) {
    return project.last_scan_at ? (
      <p className="text-xs text-muted-foreground" data-testid="stats-unavailable">
        The counts are unavailable right now.
      </p>
    ) : null;
  }
  // Locally, a GitHub remote with no PRs or issues usually means no token was given.
  const githubRemote = /github\.com[/:]/i.test(project.remote_url ?? "");
  const noForgeData = !production && githubRemote ? "No GitHub data: add a GitHub token" : "None yet";
  const zero = (n: number, why = "None yet") => (n === 0 ? why : undefined);
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5" data-testid="stats">
      <Tile
        testId="commits"
        label="Commits"
        value={formatNumber(stats.commits)}
        info="Commits WhyGraph has read from the repository's history."
        hint={zero(stats.commits)}
      />
      <Tile
        testId="described"
        label="Commits described"
        value={`${Math.round(stats.described_pct)}%`}
        info="Commits with an LLM description of what changed and why."
        hint={stats.commits === 0 ? "None yet" : `${formatNumber(stats.described)} of ${formatNumber(stats.commits)}`}
      />
      <Tile
        testId="pull-requests"
        label="Pull requests"
        value={formatNumber(stats.pull_requests)}
        info="Pull requests read from GitHub, linked to their commits."
        hint={zero(stats.pull_requests, noForgeData)}
      />
      <Tile
        testId="issues"
        label="Issues"
        value={formatNumber(stats.issues)}
        info="Issues read from GitHub that pull requests or commits refer to."
        hint={zero(stats.issues, noForgeData)}
      />
      <Tile
        testId="explained"
        label="Symbols explained"
        value={formatNumber(stats.rationale_cards)}
        info="Symbols with a rationale card: the why behind the code, as the Explorer's coverage map shows it."
        hint={zero(stats.rationale_cards)}
        // The fifth tile spans both phone columns instead of sitting alone.
        className="col-span-2 sm:col-span-1"
      >
        <Link to="/p/$slug/explorer" params={{ slug }} className="text-xs text-primary-text hover:underline">
          Open the coverage map
        </Link>
      </Tile>
    </div>
  );
}

/** The coverage-over-time line with event markers, or why it is not there yet. */
function CoverageCard({ overview }: { overview: ProjectOverview }) {
  const chart = coverageChart(overview);
  const colors = useChartColors();
  const kinds = [...new Set(overview.events.map((e) => e.kind))];
  return (
    <section className={CARD} data-testid="coverage-card" aria-labelledby="coverage-title">
      <div>
        <h2 id="coverage-title" className="text-sm font-semibold">
          Coverage over time
        </h2>
        <p className="text-xs text-muted-foreground">The share of commits with a description, after each scan.</p>
      </div>
      {chart ? (
        <>
          <Suspense fallback={<Skeleton className="h-[220px] w-full" />}>
            <ChartBlock payload={chart} valueFormat="pct" />
          </Suspense>
          {kinds.length > 0 && (
            <ul className="row-wrap text-xs text-muted-foreground" aria-label="Markers">
              {kinds.map((k) => (
                <li key={k} className="flex items-center gap-1.5">
                  <span
                    aria-hidden
                    className="size-2 rounded-full"
                    style={{ background: markerColor(colors, EVENT_TONE[k]) }}
                  />
                  {EVENT_LABEL[k]}
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <p className="text-sm text-muted-foreground" data-testid="coverage-empty">
          History starts with the next scan.
        </p>
      )}
    </section>
  );
}

/** This month's spend on the project against its budget, by task. */
function UsageCard({ usage, slug }: { usage: NonNullable<ProjectOverview["usage"]>; slug: string }) {
  const pct = usage.pct;
  const taskLabel = (task: string) => TASKS.find((t) => t.id === task)?.label ?? task;
  return (
    <section className={CARD} data-testid="usage-card" aria-labelledby="usage-title">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="usage-title" className="text-sm font-semibold">
          Usage this month
        </h2>
        <Link
          to="/p/$slug/settings"
          params={{ slug }}
          search={{ section: "budgets" }}
          className="text-xs text-primary-text hover:underline"
        >
          Usage and budgets
        </Link>
      </div>
      <p className="text-sm">
        <span className="text-2xl font-semibold tabular-nums">{formatUsd(usage.spent_usd)}</span>
        <span className="text-muted-foreground">
          {usage.budget_usd !== null ? ` of ${formatUsd(usage.budget_usd)} (${formatPct(pct)})` : " spent, no budget set"}
        </span>
      </p>
      {usage.budget_usd !== null && <SpendBar pct={pct} label="Monthly budget used" />}
      {usage.by_task.length > 0 ? (
        <ul className="flex flex-col gap-1 text-xs">
          {usage.by_task.map((t) => (
            <li key={t.task} className="flex items-center justify-between gap-3">
              <span className="text-muted-foreground">
                {taskLabel(t.task)} · {plural(t.calls, "call")}
              </span>
              <span className="tabular-nums">{formatUsd(t.cost_usd)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">Nothing spent on this project this month.</p>
      )}
    </section>
  );
}

/** Agent calls in the last 30 days, or the setup card when there were none. */
function AgentActivity({
  agents,
  production,
  setup,
}: {
  agents: ProjectOverview["agents"];
  production: boolean;
  setup: ReactNode;
}) {
  if (agents.total_calls === 0) {
    return (
      setup ?? (
        <section className={CARD} data-testid="agent-activity" aria-labelledby="agents-title">
          <h2 id="agents-title" className="text-sm font-semibold">
            Agent activity
          </h2>
          <p className="text-sm text-muted-foreground">No agent called this project in the last 30 days.</p>
        </section>
      )
    );
  }
  const llm =
    agents.llm_calls > 0
      ? `, ${formatNumber(agents.llm_calls)} used the LLM${agents.llm_cost_usd !== null ? ` (~${formatUsd(agents.llm_cost_usd)})` : ""}`
      : "";
  return (
    <section className={CARD} data-testid="agent-activity" aria-labelledby="agents-title">
      <div>
        <h2 id="agents-title" className="text-sm font-semibold">
          Agent activity
        </h2>
        <p className="text-xs text-muted-foreground" data-testid="agent-summary">
          {plural(agents.total_calls, "agent call")} in 30 days{llm}.
          {agents.last_call_day && ` Last call ${formatDate(`${agents.last_call_day}T12:00:00Z`)}.`}
        </p>
      </div>
      <Suspense fallback={<Skeleton className="h-[220px] w-full" />}>
        <ChartBlock payload={agentChart(agents.days, production)} />
      </Suspense>
      {agents.by_kind.length > 0 && (
        <div className="flex flex-col gap-1">
          <h3 className="text-xs font-medium text-muted-foreground">What agents asked for</h3>
          <ul className="flex flex-col gap-1 text-xs" data-testid="agent-kinds">
            {agents.by_kind.map((k) => (
              <li key={k.kind} className="flex items-center justify-between gap-3">
                <span>{agentKindLabel(k.kind)}</span>
                <span className="tabular-nums text-muted-foreground">{formatNumber(k.calls)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {agents.people && agents.people.length > 0 && (
        <div className="flex flex-col gap-1">
          <h3 className="text-xs font-medium text-muted-foreground">People</h3>
          <ul className="flex flex-col gap-1 text-xs" data-testid="agent-people">
            {agents.people.map((p) => (
              <li key={p.label} className="flex items-center justify-between gap-3">
                <span className="min-w-0 truncate">{p.label}</span>
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {plural(p.calls, "call")}
                  {p.last_day && ` · ${formatDate(`${p.last_day}T12:00:00Z`)}`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {agents.connections && agents.connections.length > 0 && (
        <div className="flex flex-col gap-1">
          <h3 className="text-xs font-medium text-muted-foreground">Connected portals</h3>
          <ul className="flex flex-col gap-1 text-xs" data-testid="agent-connections">
            {agents.connections.map((c, i) => (
              <li key={`${c.client_name}-${i}`} className="flex items-center justify-between gap-3">
                <span className="min-w-0 truncate">
                  {c.client_name}
                  {c.user_label && <span className="text-muted-foreground"> · {c.user_label}</span>}
                </span>
                <span className="shrink-0 text-muted-foreground">
                  {c.last_used_at ? `used ${formatRelative(c.last_used_at)}` : "never used"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {setup && (
        <details className="text-sm">
          <summary className="cursor-pointer text-xs text-primary-text select-none hover:underline">
            Connect another agent
          </summary>
          <div className="mt-3">{setup}</div>
        </details>
      )}
    </section>
  );
}

function RecentScans({
  slug,
  runs,
  loading,
  error,
  onRetry,
}: {
  slug: string;
  runs: ScanRunRow[];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const columns: Column<ScanRunRow>[] = [
    {
      key: "scan",
      header: "Scan",
      primary: true,
      cell: (run) => (
        <Link
          to="/p/$slug/scans/{-$runId}"
          params={{ slug, runId: String(run.id) }}
          className="text-primary-text hover:underline"
          title={runTitle(run)}
        >
          {runLabel(run)}
        </Link>
      ),
    },
    { key: "status", header: "Status", cell: (run) => <RunStatusBadge status={run.status} /> },
    {
      key: "when",
      header: "Started",
      cell: (run) => <span className="text-muted-foreground">{timeAgo(run.started_at ?? run.queued_at ?? null) ?? "-"}</span>,
    },
    {
      key: "duration",
      header: "Duration",
      align: "right",
      hideBelow: "md",
      cell: (run) => {
        const s = runSeconds(run);
        return <span className="text-muted-foreground">{s !== null ? formatSeconds(s) : "-"}</span>;
      },
    },
  ];
  return (
    <section className={CARD} data-testid="recent-scans" aria-labelledby="recent-title">
      <div className="flex items-center justify-between">
        <h2 id="recent-title" className="text-sm font-semibold">
          Recent scans
        </h2>
        <Link to="/p/$slug/scans/{-$runId}" params={{ slug }} className="text-xs text-primary-text hover:underline">
          View all
        </Link>
      </div>
      {loading ? (
        <TableSkeleton rows={3} cols={4} label="Loading scans" />
      ) : error ? (
        <ErrorState error={error} size="inline" onRetry={onRetry} />
      ) : (
        <ResponsiveTable
          columns={columns}
          rows={runs.slice(0, 5)}
          rowKey={(run) => String(run.id)}
          empty={<p className="text-sm text-muted-foreground">No scans yet.</p>}
        />
      )}
    </section>
  );
}

/**
 * False when the subtitle would only repeat the name: a local repo directly in a
 * shared folder prints as its folder name, which is usually the project's name.
 */
function hasSubtitle(project: ProjectDetails, shared?: string[]): boolean {
  if (project.source === "platform") return !!project.link;
  if (project.github_full_name && (project.root === null || project.source === "github")) return true;
  return !!project.root && !pathRepeatsName(project.root, shared, project);
}

/** Where the project lives: GitHub in production, the folder locally, the platform for a linked project. */
function Subtitle({ project, shared }: { project: ProjectDetails; shared?: string[] }) {
  if (project.source === "platform") {
    const link = project.link;
    return link ? (
      <span className="truncate">
        {platformHost(link.platform_origin)} · {link.org}/{link.remote_slug}
      </span>
    ) : null;
  }
  if (project.github_full_name && (project.root === null || project.source === "github")) {
    return (
      <a
        href={`https://github.com/${project.github_full_name}`}
        target="_blank"
        rel="noreferrer"
        className="inline-flex min-w-0 items-center gap-1 font-mono text-primary-text hover:underline"
        data-testid="github-link"
      >
        <span className="truncate">{project.github_full_name}</span>
        <ExternalLinkIcon className="size-3 shrink-0" aria-hidden />
      </a>
    );
  }
  return project.root ? (
    <span className="flex min-w-0 max-w-full">
      <PathText path={project.root} base={shared} />
    </span>
  ) : null;
}

/**
 * Screen 9a, the project Overview: the header (status, role, where it lives, the
 * actions its state allows), the health panel, the tiles, coverage over time,
 * this month's usage, agent activity, the describe estimate whenever commits wait,
 * and the recent scans (M2f-3 plan section 4.10).
 */
export function ProjectHome() {
  const slug = useSlug();
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
    refetchInterval: (q) => (q.state.data?.running_scan ? 3000 : false),
    // A cached "running" (say, from the wizard) may be over already: check before trusting it.
    refetchOnMount: (q) => (q.state.data?.running_scan ? "always" : true),
  });
  const p = project.data;
  const linked = p?.source === "platform";
  const ready = !!p && p.initialized && p.root_status === "ok" && !p.importing;
  const runs = useQuery({
    queryKey: projectKey(slug, "scans"),
    queryFn: () => projectApi(slug).scans(),
    enabled: !!p && (ready || !!p.importing),
    retry: false,
  });
  const overview = useQuery({
    queryKey: projectKey(slug, "project-overview"),
    queryFn: () => projectApi(slug).projectOverview(),
    enabled: !!p,
    retry: false,
  });
  const estimate = useQuery({
    queryKey: projectKey(slug, "scan-estimate"),
    queryFn: () => projectApi(slug).scanEstimate(),
    // A linked project has no describe cost: the route is refused (M2e).
    enabled: ready && !!p?.last_scan_at && !linked,
    retry: false,
  });
  const { scanNow, scanPending } = useScanActions(slug);
  const readOnly = useReadOnly();
  const portal = usePortalState().data;
  const production = isProduction(portal);
  const [dismissed, setDismissed] = useState(false);

  if (project.isLoading) return <PageSkeleton label="Loading the project" width="narrow" className="sm:py-8" />;
  if (!p) return null;

  const unsafe = estimate.isError && projectProblem(estimate.error).kind === "unsafe_path" ? projectProblem(estimate.error) : null;
  const waiting = estimate.data?.commits ?? null;
  const health = projectHealth(p, {
    lastFailure: overview.data?.last_failure ?? null,
    waiting,
    unsafePath: !!unsafe,
  });
  // Access lost / an unsupported source refuse every scan, Describe included (OVW-3).
  const scannable = !p.access_lost && p.source_supported !== false;
  const showEstimate = ready && !linked && scannable && !dismissed && !!estimate.data && estimate.data.commits > 0;
  const link = p.link ?? null;
  const deadLink = linked && (link?.status === "revoked" || link?.status === "removed");
  const role = p.my_role && p.my_role !== "admin" ? projectRoleLabel(p.my_role) : null;
  const headerReconnect = deadLink && !!link && linkNotice(link).actions.includes("reconnect");
  // The header holds a dead link's Reconnect, so the health item does not repeat it.
  const shownHealth = headerReconnect
    ? {
        ...health,
        items: health.items.map((i) =>
          i.id === "link" ? { ...i, actions: i.actions.filter((a) => a.kind !== "reconnect") } : i,
        ),
      }
    : health;

  // "Connect your agent": hidden for a dead link or a missing folder (it could not work).
  const setup: ReactNode =
    deadLink || p.root_status !== "ok" || p.importing ? null : production ? (
      ready && !readOnly && portal?.org && portal.base_url ? (
        <UseWithAgent baseUrl={portal.base_url} org={portal.org.slug} slug={slug} />
      ) : null
    ) : p.initialized && p.mcp_url ? (
      <ConnectAgent mcpUrl={p.mcp_url} configured={p.agents} />
    ) : null;

  // The header's actions by state (OVW-3): a dead link offers only Reconnect.
  const actions: ReactNode = deadLink ? (
    headerReconnect && link ? (
      <Button render={<Link to="/link" search={reconnectSearch(link)} />}>Reconnect</Button>
    ) : null
  ) : (
    <>
      {ready && !linked && (
        <Button variant="outline" render={<Link to="/p/$slug/explorer" params={{ slug }} />}>
          Open Explorer
        </Button>
      )}
      <ScanMenu project={p} onScan={scanNow} disabled={scanPending} />
    </>
  );

  return (
    <PageContainer width="narrow" className="flex flex-col gap-5 sm:py-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        {/* Below sm the title column takes the row and the actions wrap under it. */}
        <div className="flex min-w-0 basis-full flex-col gap-1.5 sm:flex-1 sm:basis-0">
          <h1 className="text-[22px] font-semibold tracking-tight break-words">{p.name}</h1>
          <div className="row-wrap text-xs text-muted-foreground">
            <ProjectStatusBadge project={p} />
            <Badge variant="outline" className="gap-1">
              {p.source === "github" ? <GitBranchIcon /> : linked ? <CloudIcon /> : <FolderGit2Icon />}
              {p.source === "local" ? "Local" : sourceLabel(p.source)}
            </Badge>
            {p.restricted && (
              <Badge variant="outline" className="gap-1" data-testid="restricted-badge">
                <LockIcon />
                Restricted
              </Badge>
            )}
            {role && <span data-testid="my-role">Your role: {role}</span>}
            {showScannedAgo(p) && <span>scanned {timeAgo(p.last_scan_at)}</span>}
          </div>
          {hasSubtitle(p, portal?.shared_folders) && (
            <div className="flex min-w-0 text-xs text-muted-foreground" data-testid="overview-subtitle">
              <Subtitle project={p} shared={portal?.shared_folders} />
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-start gap-2">{actions}</div>
      </div>

      <ProjectPortChangeNotice slug={slug} change={p.port_change} />

      {linked && (
        <section data-testid="linked-panel" className={CARD}>
          <h2 className="text-sm font-semibold">Linked project</h2>
          {/* A link problem is the health panel's item; here only the healthy "Linked to" line. */}
          {(!link || link.status === "ok") && <LinkNotice project={p} />}
          {platformRoleLabel(p.link) && (
            <p data-testid="linked-role" className="text-xs text-muted-foreground">
              Your role on the platform project: {platformRoleLabel(p.link)}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            The history, Explorer and Chat for this project are on the platform. This machine indexes
            your checkout so your agent can tell its own changes from the platform's history.
          </p>
          {!deadLink && <PlatformButtons project={p} />}
        </section>
      )}

      <HealthPanel
        project={p}
        health={shownHealth}
        onScan={scanNow}
        scanPending={scanPending}
        unsafeProblem={unsafe}
        hideDescribe={showEstimate && !estimate.data?.cost_hidden}
      />

      {ready && !linked && <Tiles project={p} production={production} />}

      {showEstimate && estimate.data && (
        <section className={CARD} data-testid="estimate-card" aria-labelledby="estimate-title">
          <h2 id="estimate-title" className="text-sm font-semibold">
            Commits waiting for a description
          </h2>
          {/* R6: a caller without project.usage gets the count without a price (cost_hidden). */}
          <EstimateBody
            slug={slug}
            estimate={estimate.data}
            canDescribe={can(p, "project.scan_full") && p.llm_block !== "budget_exceeded"}
            canConfigure={can(p, "project.configure")}
            busy={scanPending}
            onDescribe={() => scanNow({ trigger: "describe" })}
            onLater={() => setDismissed(true)}
          />
        </section>
      )}

      {ready && !linked && overview.isSuccess && <CoverageCard overview={overview.data} />}
      {overview.data?.usage && <UsageCard usage={overview.data.usage} slug={slug} />}

      {overview.isSuccess ? (
        <AgentActivity agents={overview.data.agents} production={production} setup={setup} />
      ) : overview.isError ? (
        <>
          <ErrorState
            error={overview.error}
            title="Couldn't load the coverage and agent activity"
            size="section"
            onRetry={() => void overview.refetch()}
          />
          {setup}
        </>
      ) : (
        <Skeleton className="h-40 rounded-xl" />
      )}

      {(ready || p.importing) && (
        <RecentScans
          slug={slug}
          runs={runs.data?.runs ?? []}
          loading={runs.isLoading}
          error={runs.isError ? runs.error : null}
          onRetry={() => void runs.refetch()}
        />
      )}
    </PageContainer>
  );
}
