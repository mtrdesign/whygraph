import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { GitBranchIcon, LockIcon } from "lucide-react";
import {
  ApiError,
  githubApi,
  portalApi,
  portalKey,
  type AddProjectResult,
  type GitHubInstallation,
  type GitHubRepo,
} from "../../api";
import { authMessage } from "../../lib/authErrors";
import { hardNavigate } from "../../lib/navigation";
import { cn } from "@/lib/utils";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Skeleton } from "../ui/skeleton";

const needsConnect = (err: unknown) => err instanceof ApiError && err.code === "github_authorization_required";

/** A failed import, in the import page's words. */
export function importMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "duplicate") return "This repository is already a project in this organization.";
    if (err.code === "clone_failed") return `Cloning failed: ${err.message}`;
    // The import's own `busy` is a slug race, not a sync: the server's words fit.
    if (err.code === "busy") return err.message;
    if (err.status === 429) return "Too many imports in this organization. Try again later.";
  }
  return authMessage(err);
}

/** "Connect GitHub" / "Install / configure on GitHub": both leave for GitHub. */
function useGoToGitHub() {
  return useMutation({
    mutationFn: (install: boolean) => githubApi.authorize(install),
    onSuccess: ({ url }) => void hardNavigate(url),
  });
}

function Connect() {
  const go = useGoToGitHub();
  return (
    <div className="flex flex-col items-start gap-3 p-5" data-testid="github-connect">
      <p className="text-sm text-muted-foreground">
        Connect your GitHub account to list the repositories the WhyGraph app can read. GitHub asks you
        to authorize WhyGraph once; the authorization lasts for this session.
      </p>
      <Button onClick={() => go.mutate(false)} disabled={go.isPending}>
        <GitBranchIcon data-icon="inline-start" />
        {go.isPending ? "Opening GitHub…" : "Connect GitHub"}
      </Button>
      {go.isError && (
        <Alert variant="destructive">
          <AlertDescription>{authMessage(go.error)}</AlertDescription>
        </Alert>
      )}
    </div>
  );
}

function Repos({
  installation,
  onImported,
}: {
  installation: GitHubInstallation;
  onImported: (result: AddProjectResult) => void;
}) {
  const [filter, setFilter] = useState("");
  const repos = useInfiniteQuery({
    queryKey: portalKey("github", "repos", installation.id),
    queryFn: ({ pageParam }) => githubApi.repos(installation.id, pageParam),
    initialPageParam: 1,
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, p) => n + p.repos.length, 0);
      return last.repos.length > 0 && loaded < last.total_count ? last.page + 1 : undefined;
    },
    retry: false,
  });
  const add = useMutation({
    mutationFn: (repo: GitHubRepo) =>
      portalApi.addProject({ source: "github", installation_id: installation.id, repo_id: repo.id }),
    onSuccess: onImported,
  });

  if (repos.isLoading) return <Skeleton className="m-5 h-24" />;
  if (repos.isError) {
    if (needsConnect(repos.error)) return <Connect />;
    return (
      <Alert variant="destructive" className="m-5 w-auto" data-testid="github-repos-error">
        <AlertDescription>{authMessage(repos.error)}</AlertDescription>
      </Alert>
    );
  }
  if (add.isError && needsConnect(add.error)) return <Connect />;

  const list = repos.data?.pages.flatMap((p) => p.repos) ?? [];
  const total = repos.data?.pages.at(-1)?.total_count ?? list.length;
  const needle = filter.trim().toLowerCase();
  const shown = needle ? list.filter((r) => r.full_name.toLowerCase().includes(needle)) : list;

  return (
    <div className="flex flex-col gap-3 p-5">
      {total === 0 ? (
        <p className="text-sm text-muted-foreground">
          The app can read no repository of {installation.account_login}. Choose some under Install /
          configure on GitHub.
        </p>
      ) : (
        <>
          <Input
            type="search"
            aria-label="Filter repositories"
            placeholder="Filter the loaded repositories"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          {add.isError && (
            <Alert variant="destructive" data-testid="import-error">
              <AlertTitle>Could not import the repository</AlertTitle>
              <AlertDescription>{importMessage(add.error)}</AlertDescription>
            </Alert>
          )}
          <ul className="flex flex-col rounded-lg border border-border" aria-label="Repositories">
            {shown.map((repo) => (
              <li
                key={repo.id}
                data-testid={`repo-${repo.full_name}`}
                className="flex items-center gap-3 border-b border-border px-3.5 py-2.5 last:border-b-0"
              >
                <span className="flex min-w-0 flex-1 items-center gap-2">
                  <span className={cn("truncate font-medium", repo.imported && "text-muted-foreground")}>
                    {repo.full_name}
                  </span>
                  {repo.private && (
                    <Badge variant="outline" className="gap-1">
                      <LockIcon />
                      Private
                    </Badge>
                  )}
                </span>
                <Button
                  size="sm"
                  variant={repo.imported ? "ghost" : "outline"}
                  disabled={repo.imported || add.isPending}
                  aria-label={repo.imported ? `${repo.full_name} is already imported` : `Import ${repo.full_name}`}
                  onClick={() => add.mutate(repo)}
                >
                  {repo.imported ? "Imported" : add.isPending && add.variables?.id === repo.id ? "Importing…" : "Import"}
                </Button>
              </li>
            ))}
            {shown.length === 0 && (
              <li className="px-3.5 py-2.5 text-sm text-muted-foreground">No loaded repository matches "{filter}".</li>
            )}
          </ul>
          <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
            <span>
              {list.length} of {total} loaded
            </span>
            {repos.hasNextPage && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => void repos.fetchNextPage()}
                disabled={repos.isFetchingNextPage}
              >
                {repos.isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Production's add step (M2d-2 plan section 4.10): Import from GitHub through
 * the WhyGraph GitHub App. Without a user authorization in this session the
 * page offers "Connect GitHub"; then it lists the app's installations the user
 * can see (with "Install / configure on GitHub" to add accounts or repos) and,
 * for the chosen one, its repositories, 100 at a time, with a filter over what
 * is loaded. A repository already in this org cannot be imported again.
 */
export function GitHubImport({ onImported }: { onImported: (result: AddProjectResult) => void }) {
  const installations = useQuery({
    queryKey: portalKey("github", "installations"),
    queryFn: githubApi.installations,
    retry: false,
  });
  const [picked, setPicked] = useState<number | null>(null);
  const go = useGoToGitHub();

  if (installations.isLoading) return <Skeleton className="m-5 h-24" />;
  if (installations.isError) {
    if (needsConnect(installations.error)) return <Connect />;
    return (
      <Alert variant="destructive" className="m-5 w-auto" data-testid="github-error">
        <AlertDescription>{authMessage(installations.error)}</AlertDescription>
      </Alert>
    );
  }

  const list = installations.data?.installations ?? [];
  const current = list.find((i) => i.id === picked) ?? list[0];
  const configure = (
    <Button variant="outline" size="sm" onClick={() => go.mutate(true)} disabled={go.isPending}>
      {list.length === 0 ? "Install on GitHub" : "Install / configure on GitHub"}
    </Button>
  );

  return (
    <div className="flex flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border p-5" data-testid="github-installations">
        {list.length === 0 && (
          <p className="mr-auto text-sm text-muted-foreground">
            The WhyGraph app is not installed on any GitHub account you can see. Install it on your
            account or organization, and choose the repositories it may read.
          </p>
        )}
        {list.length > 0 && (
          <div role="radiogroup" aria-label="GitHub accounts" className="mr-auto flex flex-wrap gap-2">
            {list.map((i) => (
              <button
                key={i.id}
                type="button"
                role="radio"
                aria-checked={i.id === current?.id}
                onClick={() => setPicked(i.id)}
                className={cn(
                  "flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-sm",
                  i.id === current?.id ? "border-primary bg-primary-soft text-primary-text" : "border-border hover:bg-muted",
                )}
              >
                {i.avatar_url && <img src={i.avatar_url} alt="" className="size-5 rounded-full" />}
                {i.account_login}
              </button>
            ))}
          </div>
        )}
        {configure}
      </div>
      {go.isError && (
        <Alert variant="destructive" className="m-5 mb-0 w-auto">
          <AlertDescription>{authMessage(go.error)}</AlertDescription>
        </Alert>
      )}
      {current && <Repos key={current.id} installation={current} onImported={onImported} />}
      <div className="flex justify-end border-t border-border p-4">
        <Button type="button" variant="ghost" render={<Link to="/" />}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
