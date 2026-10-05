import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CloudIcon, FolderGit2Icon, GitBranchIcon, MoreHorizontalIcon, PlusIcon } from "lucide-react";
import { portalApi, portalKey, projectApi, type ProjectSummary } from "../api";
import { canAdmin, isProduction, useReadOnly, usePortalState, useRole } from "../lib/identity";
import { timeAgo } from "../lib/projectStatus";
import { AccessLostNotice, UnsupportedSourceNotice } from "../components/portal/AccessLost";
import { LinkNotice } from "../components/portal/LinkNotice";
import { PortChangeBanner } from "../components/portal/PortChangeNotice";
import { ProjectStatusBadge as StatusBadge } from "../components/portal/ProjectStatusBadge";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "../components/ui/empty";
import { Input } from "../components/ui/input";
import { Skeleton } from "../components/ui/skeleton";

/** Where a card's main click goes: an unfinished project resumes the wizard. */
function openTarget(p: ProjectSummary) {
  return p.initialized
    ? ({ to: "/p/$slug", params: { slug: p.slug } } as const)
    : ({ to: "/p/$slug/init", params: { slug: p.slug } } as const);
}

function ProjectCard({ project }: { project: ProjectSummary }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const target = openTarget(project);
  const readOnly = useReadOnly();
  const scanned = timeAgo(project.last_scan_at);
  const usable = project.initialized && project.root_status === "ok";
  const scannable = usable && !project.access_lost && project.source_supported !== false;

  const refresh = () => queryClient.invalidateQueries({ queryKey: portalKey("projects") });
  const scan = useMutation({
    mutationFn: () => projectApi(project.slug).requestScan({ trigger: "manual" }),
    onSuccess: ({ run_id }) => {
      toast.success(`Scan queued for ${project.name}`);
      void refresh();
      void navigate({ to: "/p/$slug/scans/{-$runId}", params: { slug: project.slug, runId: String(run_id) } });
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <li
      data-testid={`project-${project.slug}`}
      className="group relative flex flex-col gap-3 rounded-xl border border-border bg-card p-4 transition-colors hover:border-primary/40"
    >
      <div className="flex items-start gap-3">
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
          <span className="truncate font-mono text-xs text-muted-foreground" title={project.remote_url ?? project.root}>
            {project.remote_url ? project.remote_url.replace(/^https?:\/\//, "") : project.root}
          </span>
        </div>
        <Badge variant="outline" className="gap-1">
          {project.source === "github" ? <GitBranchIcon /> : project.source === "platform" ? <CloudIcon /> : <FolderGit2Icon />}
          {project.source === "github" ? "GitHub" : project.source === "platform" ? "Platform" : "Local"}
        </Badge>
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={`Actions for ${project.name}`}
            className="relative z-10 -mr-1 flex size-7 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            <MoreHorizontalIcon className="size-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {!readOnly && (
              <DropdownMenuItem disabled={!scannable || scan.isPending} onClick={() => scan.mutate()}>
                Scan now
              </DropdownMenuItem>
            )}
            <DropdownMenuItem
              onClick={() => navigate({ to: "/p/$slug/settings", params: { slug: project.slug } })}
            >
              Settings
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <AccessLostNotice project={project} />
      <UnsupportedSourceNotice project={project} />
      <LinkNotice project={project} />
      <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
        <StatusBadge project={project} />
        <span>{scanned ? `Scanned ${scanned}` : project.initialized ? "Never scanned" : ""}</span>
      </div>
    </li>
  );
}

/**
 * Screen 2: every project as a card (source, linked repo, a status badge, last
 * scan), with the empty state pointing at the add wizard. A card for an
 * uninitialized project resumes the wizard at Initialize.
 */
export function ProjectsPage() {
  const projects = useQuery({
    queryKey: portalKey("projects"),
    queryFn: portalApi.projects,
    // Scans change status under the page; keep it fresh without a manual reload.
    refetchInterval: (q) => (q.state.data?.projects.some((p) => p.running_scan) ? 3000 : false),
  });
  const production = isProduction(usePortalState().data);
  const role = useRole();
  // Local mode's single user always may; in production owners and admins import.
  const canAdd = !production || canAdmin(role);
  const [filter, setFilter] = useState("");
  const list = projects.data?.projects ?? [];
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? list.filter((p) => `${p.name} ${p.remote_url ?? ""} ${p.root}`.toLowerCase().includes(needle))
    : list;

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-5 p-6 sm:p-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">Projects</h1>
          {projects.isSuccess && list.length > 0 && (
            <p className="text-[13px] text-muted-foreground">
              {list.length} project{list.length === 1 ? "" : "s"}
            </p>
          )}
        </div>
        {list.length > 0 && canAdd && (
          <Button render={<Link to="/projects/new" />}>
            <PlusIcon data-icon="inline-start" />
            New project
          </Button>
        )}
      </div>

      <PortChangeBanner />

      {projects.isLoading && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Skeleton className="h-24 rounded-xl" />
          <Skeleton className="h-24 rounded-xl" />
        </div>
      )}
      {projects.isError && (
        <p className="text-sm text-destructive">Failed to load projects: {projects.error.message}</p>
      )}

      {projects.isSuccess && list.length === 0 && (
        <Empty className="border border-dashed border-border py-16">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <FolderGit2Icon />
            </EmptyMedia>
            <EmptyTitle>No projects yet</EmptyTitle>
            <EmptyDescription>
              {!production
                ? "Add a repository from a shared folder and WhyGraph will index its history."
                : canAdd
                  ? "Import a repository from GitHub and WhyGraph will index its history."
                  : "An owner or admin of this organization can import repositories from GitHub."}
            </EmptyDescription>
          </EmptyHeader>
          {canAdd && (
            <EmptyContent>
              <Button render={<Link to="/projects/new" />}>
                <PlusIcon data-icon="inline-start" />
                {production ? "Import from GitHub" : "Add project"}
              </Button>
            </EmptyContent>
          )}
        </Empty>
      )}

      {list.length > 6 && (
        <Input
          type="search"
          aria-label="Filter projects"
          placeholder="Filter by name or repository"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="max-w-xs"
        />
      )}
      {shown.length > 0 && (
        <ul className="grid gap-3 sm:grid-cols-2">
          {shown.map((p) => (
            <ProjectCard key={p.slug} project={p} />
          ))}
        </ul>
      )}
      {list.length > 0 && shown.length === 0 && (
        <p className="text-sm text-muted-foreground">No project matches "{filter}".</p>
      )}
    </div>
  );
}
