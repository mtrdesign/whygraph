import { Link } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DownloadIcon, FolderXIcon, LinkIcon, PlayIcon } from "lucide-react";
import { portalApi, portalKey, projectKey, type ProjectDetails } from "../../api";
import type { ProjectProblem } from "../../lib/errors";
import { usePortalState } from "../../lib/identity";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { CommandBlock } from "../layout/CommandBlock";
import { PathText } from "../layout/PathText";
import { NotFoundState, type NotFoundKind } from "../state/NotFoundState";

// Screen 12: the states a project (or the portal) can be in where the normal page
// cannot render. Each one says what is wrong and what fixes it.

/** A page that is not there; `kind` says which (a thin wrapper over `NotFoundState`). */
export function NotFoundPage({ kind = "page" }: { kind?: NotFoundKind }) {
  return <NotFoundState kind={kind} />;
}

/**
 * The project's folder is gone (`missing`) or is no longer a git work tree
 * (`not_git`). For a local project the portal asks `check-path` whether the folder
 * is still shared: if not, the alert carries the `whygraph up --add-folder`
 * command; if it is, the repository itself moved or was deleted.
 */
export function ProjectUnavailable({ project }: { project: ProjectDetails }) {
  const queryClient = useQueryClient();
  const slug = project.slug;
  const local = project.source === "local";
  const check = useQuery({
    queryKey: projectKey(slug, "root-check", project.root),
    queryFn: () => portalApi.checkPath(project.root ?? ""),
    enabled: local && project.root !== null && project.root_status === "missing",
    retry: false,
  });
  const recheck = () => {
    void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
    void queryClient.invalidateQueries({ queryKey: projectKey(slug, "root-check") });
  };
  const shared = usePortalState().data?.shared_folders;
  const notGit = project.root_status === "not_git";
  const unshared = local && check.data && !check.data.shared ? check.data : null;

  return (
    <Alert variant="destructive" data-testid="project-unavailable">
      <FolderXIcon />
      <AlertTitle>
        {notGit ? "This folder is no longer a git repository" : "The project folder is not available"}
      </AlertTitle>
      <AlertDescription>
        {project.root && <PathText
            path={project.root}
            // Relative to its shared folder, unless that folder is no longer shared:
            // then the whole path is what to share again.
            base={unshared ? undefined : shared}
            variant="block"
            className="text-foreground"
          />}
        {notGit ? (
          <p className="mt-1">
            The <span className="font-mono">.git</span> folder is gone. Restore it, or remove the
            project from Settings.
          </p>
        ) : !local ? (
          <p className="mt-1">
            The clone under the portal's data folder is missing. Remove the project and add the
            GitHub repository again.
          </p>
        ) : unshared ? (
          <div className="mt-1 flex flex-col gap-2" data-testid="unshared-fix">
            <p>
              The portal runs in Docker and only sees folders shared when it started. This one is
              not shared any more (it was unshared, or the repository moved). Share it again, then
              check again:
            </p>
            {unshared.command && <CommandBlock command={unshared.command} className="text-foreground" />}
          </div>
        ) : (
          <p className="mt-1">
            {check.isLoading
              ? "Checking whether the folder is shared…"
              : "The folder is shared but nothing is there: the repository was moved or deleted. Put it back, or remove the project from Settings."}
          </p>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={recheck}>
            Check again
          </Button>
          <Button
            size="sm"
            variant="ghost"
            render={<Link to="/p/$slug/settings" params={{ slug }} />}
          >
            Project settings
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}

/** The project was added but never initialized, so there is nothing to show yet. */
export function NotInitialized({ slug }: { slug: string }) {
  return (
    <Alert data-testid="not-initialized">
      <PlayIcon />
      <AlertTitle>This project is not set up yet</AlertTitle>
      <AlertDescription>
        <p>Connect your agents and run the first scan to fill the Explorer and Chat.</p>
        <div className="mt-2">
          <Button size="sm" render={<Link to="/p/$slug/init" params={{ slug }} />}>
            Finish setup
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}

/**
 * A production import that has not finished (`project.importing`): the clone and
 * its first scan run in the background, so Explorer and Chat have nothing to read
 * yet. With no run in flight the import did not finish; its run says why.
 */
export function ImportingNotice({ project }: { project: ProjectDetails }) {
  const slug = project.slug;
  const run = project.running_scan;
  const repo = project.github_full_name ?? project.name;
  return (
    <Alert data-testid="importing-notice">
      <DownloadIcon />
      <AlertTitle>{run ? `Importing ${repo}` : "The import did not finish"}</AlertTitle>
      <AlertDescription>
        <p>
          {run
            ? "WhyGraph is copying the repository and running its first scan. The Explorer and Chat open when it is done."
            : "The Explorer and Chat open once the repository is imported. The import's last run says what went wrong."}
        </p>
        <div className="mt-2">
          {run ? (
            <Button
              size="sm"
              render={<Link to="/p/$slug/scans/{-$runId}" params={{ slug, runId: String(run.id) }} />}
            >
              Follow the import
            </Button>
          ) : (
            <Button size="sm" render={<Link to="/p/$slug/scans/{-$runId}" params={{ slug }} />}>
              Open scans
            </Button>
          )}
        </div>
      </AlertDescription>
    </Alert>
  );
}

/** A refused call (`unsafe_path` and the like), with the instruction to fix it. */
export function ProblemAlert({ problem }: { problem: ProjectProblem }) {
  return (
    <Alert variant="destructive" data-testid={`problem-${problem.kind}`}>
      <LinkIcon />
      <AlertTitle>{problem.title}</AlertTitle>
      <AlertDescription>
        <p>{problem.message}</p>
        {problem.path && <p className="mt-1 font-mono text-xs text-foreground">{problem.path}</p>}
      </AlertDescription>
    </Alert>
  );
}
