import { useMemo, useState } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { CloudIcon, FolderGit2Icon, GitBranchIcon, LockIcon, MoreHorizontalIcon, PlusIcon, SearchIcon } from "lucide-react";
import { portalApi, portalKey, type ProjectSummary } from "../api";
import { canAdmin, isProduction, usePortalState, useRole } from "../lib/identity";
import { formatDate, formatNumber, formatUsd, timeAgo } from "../lib/format";
import { projectRoleLabel, sourceLabel } from "../lib/labels";
import { platformHost } from "../lib/platformLink";
import { plural } from "../lib/plural";
import { projectStatus } from "../lib/projectStatus";
import { showScannedAgo, STATUS_ORDER } from "../lib/projectHealth";
import type { ProjectSort, ProjectsSearch } from "../lib/routeSearch";
import { useScanActions } from "../lib/scanActions";
import { cn } from "../lib/utils";
import { AccessLostNotice, UnsupportedSourceNotice } from "../components/portal/AccessLost";
import { LinkNotice } from "../components/portal/LinkNotice";
import { PortChangeBanner } from "../components/portal/PortChangeNotice";
import { ProjectStatusBadge as StatusBadge } from "../components/portal/ProjectStatusBadge";
import { ScanMenuItems } from "../components/portal/ScanMenu";
import { PageContainer } from "../components/layout/PageContainer";
import { PathText, pathRepeatsName } from "../components/layout/PathText";
import { EmptyState } from "../components/state/EmptyState";
import { ErrorState } from "../components/state/ErrorState";
import { CardGridSkeleton } from "../components/state/Skeletons";
import { Skeleton } from "../components/ui/skeleton";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { Input } from "../components/ui/input";
import { nativeSelect } from "../components/portal/Field";

/** Cards rendered before "Show more" (plan section 0.3 #1). */
export const PAGE_CARDS = 60;

const SORT_LABEL: Record<ProjectSort, string> = {
  name: "Name",
  scanned: "Last scanned",
  status: "Status",
  cost: "Cost",
};

/** Where a card's main click goes: an unfinished local project resumes the wizard, an import shows its progress. */
function openTarget(p: ProjectSummary) {
  return p.initialized || p.importing
    ? ({ to: "/p/$slug", params: { slug: p.slug } } as const)
    : ({ to: "/p/$slug/init", params: { slug: p.slug } } as const);
}

/** The text the search box matches: the name and where the project lives. */
function haystack(p: ProjectSummary): string {
  return `${p.name} ${p.slug} ${p.github_full_name ?? ""} ${p.remote_url ?? ""} ${p.root ?? ""}`.toLowerCase();
}

/** The list in the order the URL asks for (Name by default). */
export function sortProjects(list: ProjectSummary[], sort: ProjectSort | undefined): ProjectSummary[] {
  const byName = (a: ProjectSummary, b: ProjectSummary) => a.name.localeCompare(b.name);
  const out = [...list];
  switch (sort) {
    case "scanned":
      return out.sort((a, b) => (b.last_scan_at ?? "").localeCompare(a.last_scan_at ?? "") || byName(a, b));
    case "status": {
      const rank = (p: ProjectSummary) => STATUS_ORDER.indexOf(projectStatus(p).key);
      return out.sort((a, b) => rank(a) - rank(b) || byName(a, b));
    }
    case "cost":
      return out.sort((a, b) => (b.usage?.month_spend_usd ?? -1) - (a.usage?.month_spend_usd ?? -1) || byName(a, b));
    default:
      return out.sort(byName);
  }
}

/** Where the project lives, by mode and source: never a server path in production. */
function Subtitle({ project, production, shared }: { project: ProjectSummary; production: boolean; shared?: string[] }) {
  if (project.source === "platform") {
    const link = project.link;
    return (
      <span className="truncate text-xs text-muted-foreground" data-testid="card-subtitle">
        {link ? `${platformHost(link.platform_origin)} · ${link.org}/${link.remote_slug}` : "Linked to a platform project"}
      </span>
    );
  }
  if (production || project.source === "github") {
    const repo = project.github_full_name ?? project.remote_url?.replace(/^https?:\/\//, "") ?? "";
    return (
      <span className="truncate font-mono text-xs text-muted-foreground" data-testid="card-subtitle">
        {repo}
      </span>
    );
  }
  // A repo directly in a shared folder would print its own name again: no subtitle.
  return project.root && !pathRepeatsName(project.root, shared, project) ? (
    <span className="flex min-w-0 text-muted-foreground" data-testid="card-subtitle">
      <PathText path={project.root} base={shared} />
    </span>
  ) : null;
}

/** "$12.40 this month" and a thin bar against the project's budget. */
function CostLine({ usage }: { usage: NonNullable<ProjectSummary["usage"]> }) {
  const pct = usage.pct;
  return (
    <div className="flex flex-col gap-1" data-testid="card-cost">
      <span className="text-xs text-muted-foreground">
        <span className="font-medium text-foreground tabular-nums">{formatUsd(usage.month_spend_usd)}</span> this month
        {usage.budget && ` of ${formatUsd(usage.budget.monthly_usd)}`}
      </span>
      {usage.budget && pct !== null && (
        <div
          className="h-1 w-full overflow-hidden rounded-full bg-track"
          role="meter"
          aria-label="Monthly budget used"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.min(100, Math.round(pct))}
        >
          <div
            className={cn("h-full rounded-full", pct >= 100 ? "bg-destructive" : pct >= 75 ? "bg-warning" : "bg-primary")}
            style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
          />
        </div>
      )}
    </div>
  );
}

function ProjectCard({
  project,
  production,
  mixed,
  shared,
}: {
  project: ProjectSummary;
  production: boolean;
  mixed: boolean;
  shared?: string[];
}) {
  const navigate = useNavigate();
  const target = openTarget(project);
  const { scanNow, scanPending } = useScanActions(project.slug);
  const stats = project.last_scan_stats;
  const scanned = showScannedAgo(project) ? timeAgo(project.last_scan_at) : null;
  const role = project.my_role && project.my_role !== "admin" ? projectRoleLabel(project.my_role) : null;

  return (
    <li
      data-testid={`project-${project.slug}`}
      className="group relative flex min-w-0 flex-col gap-3 rounded-xl bg-card p-4 shadow-card transition-colors hover:ring-1 hover:ring-primary/40"
    >
      <div className="flex min-w-0 items-start gap-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-sm font-semibold">
          {project.name.charAt(0).toUpperCase()}
        </span>
        <div className="flex min-w-0 flex-1 flex-col">
          <Link
            {...target}
            className="truncate font-medium after:absolute after:inset-0 after:rounded-xl focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-ring"
          >
            {project.name}
          </Link>
          <Subtitle project={project} production={production} shared={shared} />
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={`Actions for ${project.name}`}
            className="relative z-10 -mr-1 flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            <MoreHorizontalIcon className="size-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-72 max-w-[calc(100vw-2rem)]">
            <ScanMenuItems project={project} onScan={scanNow} pending={scanPending} />
            {(project.permissions?.includes("project.scan") || project.permissions?.includes("project.scan_full")) && (
              <DropdownMenuSeparator />
            )}
            <DropdownMenuItem onClick={() => navigate({ to: "/p/$slug/settings", params: { slug: project.slug } })}>
              Settings
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className="row-wrap">
        <StatusBadge project={project} />
        {project.restricted && (
          <Badge variant="outline" className="gap-1" data-testid="restricted-badge">
            <LockIcon />
            Restricted
          </Badge>
        )}
        {mixed && (
          <Badge variant="outline" className="gap-1" data-testid="source-badge">
            {project.source === "github" ? <GitBranchIcon /> : project.source === "platform" ? <CloudIcon /> : <FolderGit2Icon />}
            {project.source === "local" ? "Local" : sourceLabel(project.source)}
          </Badge>
        )}
        {role && (
          <Badge variant="secondary" data-testid="role-badge">
            {role}
          </Badge>
        )}
        {scanned ? (
          <span className="text-xs text-muted-foreground">Scanned {scanned}</span>
        ) : (
          project.initialized &&
          !project.last_scan_at &&
          !project.running_scan && <span className="text-xs text-muted-foreground">Never scanned</span>
        )}
      </div>

      <AccessLostNotice project={project} />
      <UnsupportedSourceNotice project={project} />
      <LinkNotice project={project} />

      {stats && (
        <p
          className="text-xs text-muted-foreground tabular-nums"
          data-testid="card-stats"
          title={`As of the last scan, ${formatDate(stats.as_of)}`}
        >
          {plural(stats.commits, "commit")} · {Math.round(stats.described_pct)}% described ·{" "}
          {formatNumber(stats.rationale_cards)} {stats.rationale_cards === 1 ? "symbol" : "symbols"} explained
        </p>
      )}
      {/* No "$0.00 this month" on a project with nothing spent and no budget to measure against. */}
      {project.usage && (project.usage.month_spend_usd > 0 || project.usage.budget) && <CostLine usage={project.usage} />}
    </li>
  );
}

/**
 * Until S20's first-run checklist lands, the owner's empty state: still "No
 * projects yet" with the add action, in the slot the checklist takes over.
 */
function ChecklistPlaceholder({ production }: { production: boolean }) {
  return (
    <div data-testid="first-run-slot">
      <EmptyState
        icon={<FolderGit2Icon />}
        title="No projects yet"
        description={
          production
            ? "Import a repository from GitHub and WhyGraph will index its history."
            : "Add a repository from a shared folder and WhyGraph will index its history."
        }
        action={
          <Button render={<Link to="/projects/new" />}>
            <PlusIcon data-icon="inline-start" />
            {production ? "Import from GitHub" : "Add project"}
          </Button>
        }
        className="border border-dashed border-border py-16"
      />
    </div>
  );
}

/**
 * Screen 2: every project as a card (status from the shared precedence, where it
 * lives, the last scan's stats, this month's cost, the caller's role), with
 * search, sort and "Show more" in the URL-backed header, and an empty state for
 * each audience.
 */
export function ProjectsPage() {
  const projects = useQuery({
    queryKey: portalKey("projects"),
    queryFn: portalApi.projects,
    // Scans change status under the page; keep it fresh without a manual reload.
    refetchInterval: (q) => (q.state.data?.projects.some((p) => p.running_scan) ? 3000 : false),
  });
  const state = usePortalState().data;
  const production = isProduction(state);
  const role = useRole();
  // Local mode's single user always may; in production owners and admins import.
  const canAdd = !production || canAdmin(role);
  const search = useSearch({ strict: false }) as ProjectsSearch;
  const navigate = useNavigate();
  const [limit, setLimit] = useState(PAGE_CARDS);
  const list = useMemo(() => projects.data?.projects ?? [], [projects.data]);
  const needle = (search.q ?? "").trim().toLowerCase();
  const hasCost = list.some((p) => p.usage);
  const sort = search.sort === "cost" && !hasCost ? undefined : search.sort;
  const shown = useMemo(
    () => sortProjects(needle ? list.filter((p) => haystack(p).includes(needle)) : list, sort),
    [list, needle, sort],
  );
  const mixed = new Set(list.map((p) => p.source)).size > 1;

  const setSearch = (next: ProjectsSearch) =>
    void navigate({ to: "/", search: { q: search.q, sort: search.sort, ...next }, replace: true });

  const addButton = canAdd && (
    <Button render={<Link to="/projects/new" />}>
      <PlusIcon data-icon="inline-start" />
      {production ? "Import" : "New project"}
    </Button>
  );

  return (
    <PageContainer className="flex flex-col gap-5 sm:py-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[22px] font-semibold tracking-tight">Projects</h1>
          <p className="text-[13px] text-muted-foreground" data-testid="projects-count">
            {projects.isSuccess ? plural(list.length, "project") : "\u00a0"}
          </p>
        </div>
        {addButton}
      </div>

      <PortChangeBanner />

      {list.length >= 2 && (
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-0 flex-1 sm:max-w-xs">
            <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              type="search"
              aria-label="Search projects"
              placeholder="Search projects"
              defaultValue={search.q ?? ""}
              onChange={(e) => setSearch({ q: e.target.value || undefined })}
              className="pl-8"
            />
          </div>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            Sort
            <select
              aria-label="Sort projects"
              value={sort ?? "name"}
              onChange={(e) => {
                const value = e.target.value as ProjectSort;
                setSearch({ sort: value === "name" ? undefined : value });
              }}
              className={nativeSelect("w-auto")}
            >
              {(Object.keys(SORT_LABEL) as ProjectSort[])
                .filter((k) => k !== "cost" || hasCost)
                .map((k) => (
                  <option key={k} value={k}>
                    {SORT_LABEL[k]}
                  </option>
                ))}
            </select>
          </label>
        </div>
      )}

      {projects.isLoading && (
        <>
          {/* The search / sort row and cards at the real height, so nothing jumps when the list lands (ER-5). */}
          <div className="flex flex-wrap items-center gap-2" aria-hidden="true">
            <Skeleton className="h-8 min-w-0 flex-1 rounded-lg sm:max-w-xs" />
            <Skeleton className="h-8 w-36 rounded-lg" />
          </div>
          <CardGridSkeleton count={4} cols={2} cardClassName="h-31" label="Loading projects" />
        </>
      )}
      {projects.isError && (
        <ErrorState error={projects.error} title="Couldn't load projects" onRetry={() => void projects.refetch()} />
      )}

      {projects.isSuccess && list.length === 0 &&
        (canAdd ? (
          <ChecklistPlaceholder production={production} />
        ) : role === "reader" ? (
          <EmptyState
            icon={<FolderGit2Icon />}
            title="No projects"
            description="This organization has no projects."
            className="border border-dashed border-border py-16"
          />
        ) : (
          <EmptyState
            icon={<FolderGit2Icon />}
            title="No projects shared with you yet"
            description="An owner or admin can give you access."
            className="border border-dashed border-border py-16"
          />
        ))}

      {shown.length > 0 && (
        <ul className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
          {shown.slice(0, limit).map((p) => (
            <ProjectCard
              key={p.slug}
              project={p}
              production={production}
              mixed={mixed}
              shared={state?.shared_folders}
            />
          ))}
        </ul>
      )}
      {shown.length > limit && (
        <div className="flex justify-center">
          <Button variant="outline" onClick={() => setLimit((n) => n + PAGE_CARDS)}>
            Show more ({shown.length - limit} more)
          </Button>
        </div>
      )}
      {list.length > 0 && shown.length === 0 && (
        <p className="text-sm text-muted-foreground">No project matches "{search.q}".</p>
      )}
    </PageContainer>
  );
}
