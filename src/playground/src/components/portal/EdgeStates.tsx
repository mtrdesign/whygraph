import { Link } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderXIcon, LinkIcon, PlayIcon } from "lucide-react";
import { portalApi, portalKey, projectKey, type ProjectDetails } from "../../api";
import type { ProjectProblem } from "../../lib/errors";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { CopyButton } from "./CopyButton";
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
    queryFn: () => portalApi.checkPath(project.root),
    enabled: local && project.root_status === "missing",
    retry: false,
  });
  const recheck = () => {
    void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
    void queryClient.invalidateQueries({ queryKey: projectKey(slug, "root-check") });
  };
  const notGit = project.root_status === "not_git";
  const unshared = local && check.data && !check.data.shared ? check.data : null;

  return (
    <Alert variant="destructive" data-testid="project-unavailable">
      <FolderXIcon />
      <AlertTitle>
        {notGit ? "This folder is no longer a git repository" : "The project folder is not available"}
      </AlertTitle>
      <AlertDescription>
        <p className="font-mono text-xs text-foreground">{project.root}</p>
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
            {unshared.command && (
              <div className="flex flex-wrap items-center gap-2">
                <code className="min-w-0 flex-1 overflow-x-auto rounded-md bg-muted px-2 py-1 font-mono text-xs text-foreground">
                  {unshared.command}
                </code>
                <CopyButton text={unshared.command} />
              </div>
            )}
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
