import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { GitBranchIcon, LockIcon, RefreshCwIcon } from "lucide-react";
import { toast } from "sonner";
import {
  ApiError,
  authApi,
  githubApi,
  portalApi,
  portalKey,
  projectKey,
  type AddProjectResult,
  type GitHubInstallation,
  type GitHubRepo,
  type InstallationRepos,
} from "../../api";
import { authMessage } from "../../lib/authErrors";
import { errorMessage } from "../../lib/apiErrors";
import { hardNavigate } from "../../lib/navigation";
import { plural } from "../../lib/plural";
import { cn } from "@/lib/utils";
import { DisabledReason } from "../state/DisabledReason";
import { Alert, AlertDescription } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Checkbox } from "../ui/checkbox";
import { Input } from "../ui/input";
import { Skeleton } from "../ui/skeleton";

const SEARCH_DEBOUNCE_MS = 250;

const needsConnect = (err: unknown) => err instanceof ApiError && err.code === "github_authorization_required";
const needsGitHubIdentity = (err: unknown) => err instanceof ApiError && err.code === "github_required";

/** A failed import, in the registry's words (the `github` context). */
export function importMessage(err: unknown): string {
  if (err instanceof ApiError) return errorMessage(err, "github");
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

/** The bootstrap owner has no GitHub identity, and importing needs one (ONB-2). */
function GitHubRequired() {
  const start = useMutation({
    mutationFn: () => authApi.githubStart({ next: window.location.href }),
    onSuccess: ({ authorize_url }) => hardNavigate(authorize_url),
  });
  return (
    <div className="flex flex-col items-start gap-3 p-5" data-testid="github-required">
      <p className="text-sm text-muted-foreground">
        Importing needs a GitHub sign-in. You're signed in with the bootstrap password account. Sign in
        with GitHub, or make a GitHub user an owner in Members.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => start.mutate()} disabled={start.isPending || start.isSuccess}>
          <GitBranchIcon data-icon="inline-start" />
          {start.isPending || start.isSuccess ? "Opening GitHub…" : "Sign in with GitHub"}
        </Button>
        <Button variant="outline" render={<Link to="/members" />}>
          Open Members
        </Button>
      </div>
      {start.isError && (
        <Alert variant="destructive">
          <AlertDescription>{authMessage(start.error)}</AlertDescription>
        </Alert>
      )}
    </div>
  );
}

/** An account's avatar, or its initials when GitHub gave none (or it does not load). */
function AccountAvatar({ login, url }: { login: string; url: string | null }) {
  const [broken, setBroken] = useState(false);
  if (url && !broken) {
    return <img src={url} alt="" className="size-6 shrink-0 rounded-full" onError={() => setBroken(true)} />;
  }
  return (
    <span
      aria-hidden="true"
      data-testid="avatar-initials"
      className="flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-semibold uppercase text-muted-foreground"
    >
      {login.slice(0, 2)}
    </span>
  );
}

/** One repository of the selection, as the submit loop sees it. */
type RowStatus =
  | { state: "waiting" }
  | { state: "starting" }
  | { state: "started"; slug: string; runId: number | null; scanError?: string }
  | { state: "failed"; message: string }
  | { state: "not_started"; message: string };

const ROW_LABEL: Record<RowStatus["state"], string> = {
  waiting: "Waiting",
  starting: "Starting",
  started: "Started",
  failed: "Failed",
  not_started: "Not started",
};

function ImportResults({
  selected,
  status,
  done,
  onBack,
}: {
  selected: GitHubRepo[];
  status: Record<number, RowStatus>;
  done: boolean;
  onBack: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 p-5" data-testid="import-results">
      <h2 className="text-sm font-medium">
        {done ? `Imported ${plural(selected.length, "repository", "repositories")}` : "Importing…"}
      </h2>
      <ul className="flex flex-col rounded-lg border border-border" aria-label="Import results">
        {selected.map((repo) => {
          const s = status[repo.id] ?? { state: "waiting" };
          return (
            <li
              key={repo.id}
              data-testid={`import-${repo.full_name}`}
              className="row-wrap flex items-center gap-3 border-b border-border px-3.5 py-2.5 last:border-b-0"
            >
              <span className="min-w-0 flex-1 truncate font-medium">{repo.full_name}</span>
              <span
                className={cn(
                  "text-sm",
                  s.state === "failed" || s.state === "not_started" ? "text-destructive" : "text-muted-foreground",
                )}
              >
                <span className="font-medium">{ROW_LABEL[s.state]}</span>
                {(s.state === "failed" || s.state === "not_started") && (
                  <span data-testid="import-error">{`: ${s.message}`}</span>
                )}
              </span>
              {s.state === "started" && (
                <Button
                  size="sm"
                  variant="outline"
                  render={
                    <Link
                      to="/p/$slug/init"
                      params={{ slug: s.slug }}
                      search={{ step: "configure", ...(s.runId ? { run: s.runId } : {}) }}
                    />
                  }
                >
                  Configure
                </Button>
              )}
            </li>
          );
        })}
      </ul>
      {done && (
        <div className="flex flex-wrap gap-2">
          <Button render={<Link to="/" />}>Go to projects</Button>
          <Button variant="outline" onClick={onBack}>
            Back to the list
          </Button>
        </div>
      )}
    </div>
  );
}

function Repos({ installation, onInstallMore }: { installation: GitHubInstallation; onInstallMore: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Map<number, GitHubRepo>>(new Map());
  const [status, setStatus] = useState<Record<number, RowStatus> | null>(null);
  const [submitted, setSubmitted] = useState<GitHubRepo[]>([]);
  const [done, setDone] = useState(false);
  const [reconnect, setReconnect] = useState(false);
  const lastRefresh = useRef(0);

  useEffect(() => {
    const id = window.setTimeout(() => setQ(input.trim()), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [input]);

  const queryKey = portalKey("github", "repos", installation.id, q);
  const repos = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => githubApi.repos(installation.id, { q: q || undefined, page: pageParam }),
    initialPageParam: 1,
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, p) => n + p.repos.length, 0);
      return last.repos.length > 0 && loaded < last.total_count ? last.page + 1 : undefined;
    },
    placeholderData: (prev) => prev,
    retry: false,
  });
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects, staleTime: 10_000 });
  const slugOf = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of projects.data?.projects ?? []) if (p.github_full_name) m.set(p.github_full_name, p.slug);
    return m;
  }, [projects.data]);

  const refresh = useMutation({
    mutationFn: () => githubApi.repos(installation.id, { q: q || undefined, refresh: true, page: 1 }),
    onSuccess: (first) => {
      queryClient.setQueryData(queryKey, { pages: [first], pageParams: [1] });
      lastRefresh.current = Date.now();
    },
  });

  if (reconnect) return <Connect />;
  if (repos.isLoading) return <Skeleton className="m-5 h-24" />;
  if (repos.isError) {
    if (needsConnect(repos.error)) return <Connect />;
    return (
      <Alert variant="destructive" className="m-5 w-auto" data-testid="github-repos-error">
        <AlertDescription>{authMessage(repos.error)}</AlertDescription>
      </Alert>
    );
  }

  const pages = repos.data?.pages ?? [];
  const list = pages.flatMap((p) => p.repos);
  const last: InstallationRepos | undefined = pages.at(-1);
  const total = last?.total_count ?? list.length;
  const importsLeft = last?.imports_left;
  const cap = importsLeft === undefined ? Infinity : Math.max(0, importsLeft);
  const full = selected.size >= cap;
  const unfiltered = q === "";
  const allImported = unfiltered && total > 0 && !repos.hasNextPage && list.every((r) => r.imported);

  const toggle = (repo: GitHubRepo, on: boolean) =>
    setSelected((prev) => {
      const next = new Map(prev);
      if (on) next.set(repo.id, repo);
      else next.delete(repo.id);
      return next;
    });

  const submit = async () => {
    const queue = [...selected.values()];
    setSubmitted(queue);
    setDone(false);
    setStatus(Object.fromEntries(queue.map((r) => [r.id, { state: "waiting" } as RowStatus])));
    const set = (id: number, s: RowStatus) => setStatus((prev) => ({ ...(prev ?? {}), [id]: s }));
    let stopped: string | null = null;
    const results: { repo: GitHubRepo; result: AddProjectResult }[] = [];
    for (const repo of queue) {
      if (stopped) {
        set(repo.id, { state: "not_started", message: stopped });
        continue;
      }
      set(repo.id, { state: "starting" });
      try {
        const result = await portalApi.addProject({
          source: "github",
          installation_id: installation.id,
          repo_id: repo.id,
        });
        queryClient.setQueryData(projectKey(result.project.slug, "project"), result.project);
        results.push({ repo, result });
        set(repo.id, {
          state: "started",
          slug: result.project.slug,
          runId: result.initial_run_id ?? null,
          scanError: result.scan_error,
        });
      } catch (err) {
        set(repo.id, { state: "failed", message: importMessage(err) });
        if (err instanceof ApiError && err.status === 429) stopped = "the import limit was reached before this one.";
        else if (needsConnect(err)) {
          stopped = "GitHub needs to be connected again.";
          setReconnect(true);
        }
      }
    }
    void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
    void queryClient.invalidateQueries({ queryKey: portalKey("github") });
    setSelected(new Map());
    setDone(true);
    if (queue.length === 1 && results.length === 1) {
      const { result } = results[0];
      toast.success(`Imported ${result.project.name}`);
      void navigate({
        to: "/p/$slug/init",
        params: { slug: result.project.slug },
        search: { step: "configure", ...(result.initial_run_id ? { run: result.initial_run_id } : {}) },
      });
    }
  };

  if (status) {
    return (
      <ImportResults
        selected={submitted}
        status={status}
        done={done}
        onBack={() => {
          setStatus(null);
          setSubmitted([]);
        }}
      />
    );
  }

  const note =
    importsLeft === undefined ? null : importsLeft === 0
      ? "You've reached the import limit for this hour."
      : `You can import ${importsLeft} more this hour.`;

  return (
    <div className="flex flex-col">
      <div className="flex flex-col gap-3 p-5">
        {total === 0 && unfiltered ? (
          <p className="text-sm text-muted-foreground">
            The app can read no repository of {installation.account_login}. Choose some under Install /
            configure on GitHub.
          </p>
        ) : (
          <>
            <div className="flex items-center gap-2">
              <Input
                type="search"
                aria-label="Search repositories"
                placeholder="Search repositories"
                value={input}
                onChange={(e) => setInput(e.target.value)}
              />
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => refresh.mutate()}
                disabled={refresh.isPending}
              >
                <RefreshCwIcon data-icon="inline-start" />
                Refresh
              </Button>
            </div>
            {allImported && (
              <Alert variant="info" data-testid="all-imported">
                <AlertDescription className="flex flex-wrap items-center gap-3">
                  Every repository this installation covers is already a project here.
                  <Button size="sm" variant="outline" onClick={onInstallMore}>
                    Install on more repositories
                  </Button>
                </AlertDescription>
              </Alert>
            )}
            {refresh.isError && (
              <Alert variant="destructive">
                <AlertDescription>{importMessage(refresh.error)}</AlertDescription>
              </Alert>
            )}
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
              <span data-testid="repo-count">{plural(total, "repository", "repositories")}</span>
              {note && <span data-testid="imports-left">{note}</span>}
            </div>
            <ul className="flex flex-col rounded-lg border border-border" aria-label="Repositories">
              {list.map((repo) => {
                const slug = slugOf.get(repo.full_name);
                const on = selected.has(repo.id);
                const blocked = !on && full;
                const box = (
                  <Checkbox
                    aria-label={`Select ${repo.full_name}`}
                    checked={on}
                    disabled={blocked}
                    onCheckedChange={(v) => toggle(repo, v === true)}
                  />
                );
                return (
                  <li
                    key={repo.id}
                    data-testid={`repo-${repo.full_name}`}
                    className={cn(
                      "row-wrap flex items-center gap-3 border-b border-border px-3.5 py-2.5 last:border-b-0",
                      on && "bg-primary-soft text-primary-text",
                    )}
                  >
                    {repo.imported ? (
                      <span className="size-4 shrink-0" aria-hidden="true" />
                    ) : blocked ? (
                      <DisabledReason reason={note ?? "You can't import more repositories right now."}>
                        {box}
                      </DisabledReason>
                    ) : (
                      box
                    )}
                    <span className="flex min-w-0 flex-1 items-center gap-2">
                      {repo.imported && slug ? (
                        <Link
                          to="/p/$slug"
                          params={{ slug }}
                          className="truncate font-medium text-muted-foreground underline-offset-2 hover:underline"
                        >
                          {repo.full_name}
                        </Link>
                      ) : (
                        <span className={cn("truncate font-medium", repo.imported && "text-muted-foreground")}>
                          {repo.full_name}
                        </span>
                      )}
                      {repo.private && (
                        <Badge variant="outline" className="gap-1">
                          <LockIcon />
                          Private
                        </Badge>
                      )}
                    </span>
                    {repo.imported && <Badge variant="outline">Imported</Badge>}
                  </li>
                );
              })}
              {list.length === 0 && (
                <li className="px-3.5 py-2.5 text-sm text-muted-foreground">No repository matches "{q}".</li>
              )}
            </ul>
            {repos.hasNextPage && (
              <div className="flex justify-end">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void repos.fetchNextPage()}
                  disabled={repos.isFetchingNextPage}
                >
                  {repos.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              </div>
            )}
            {last?.truncated && (
              <p className="text-xs text-muted-foreground">
                Only the first repositories are listed. Search to find the rest.
              </p>
            )}
          </>
        )}
      </div>
      <Footer count={selected.size} onImport={() => void submit()} />
    </div>
  );
}

/** The sticky footer: the primary action and Cancel. */
function Footer({ count, onImport }: { count: number; onImport: (() => void) | null }) {
  return (
    <div className="sticky bottom-0 flex flex-wrap items-center justify-end gap-2 rounded-b-xl border-t border-border bg-card p-4">
      <Button type="button" variant="ghost" render={<Link to="/" />}>
        Cancel
      </Button>
      {onImport && (
        <Button type="button" disabled={count === 0} onClick={onImport}>
          {count === 0 ? "Import" : `Import ${plural(count, "repository", "repositories")}`}
        </Button>
      )}
    </div>
  );
}

/**
 * Production's Source step: import from GitHub through the WhyGraph GitHub App.
 * Without a user authorization in this session the page offers "Connect GitHub";
 * then it lists the app's installations the user can see (with "Install /
 * configure on GitHub" to add accounts or repos) and, for the chosen one, its
 * repositories: a server-side search, 100 per page, a checkbox per repository
 * (capped at what the org may still import this hour) and one sticky "Import N
 * repositories". The selection is sent one repository at a time; one goes
 * straight to its Configure step, several get a summary.
 *
 * Parameters
 * ----------
 * _props : object
 *     `onImported` is no longer called: the page navigates itself (the run id
 *     rides in the URL), and the wizard page can stop passing it.
 */
export function GitHubImport(_props: { onImported?: (result: AddProjectResult) => void } = {}) {
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
    if (needsGitHubIdentity(installations.error)) return <GitHubRequired />;
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
      <div className="flex flex-col gap-2 border-b border-border p-5" data-testid="github-installations">
        {list.length === 0 && (
          <p className="text-sm text-muted-foreground">
            The WhyGraph app is not installed on any GitHub account you can see. Install it on your
            account or organization, and choose the repositories it may read.
          </p>
        )}
        {list.length > 0 && (
          <ul role="radiogroup" aria-label="GitHub accounts" className="flex flex-col gap-1">
            {list.map((i) => (
              <li key={i.id}>
                <button
                  type="button"
                  role="radio"
                  aria-label={i.account_login}
                  aria-checked={i.id === current?.id}
                  onClick={() => setPicked(i.id)}
                  className={cn(
                    "flex w-full items-center gap-2.5 rounded-md border px-2.5 py-1.5 text-left text-sm",
                    i.id === current?.id
                      ? "border-primary bg-primary-soft text-primary-text"
                      : "border-border hover:bg-muted",
                  )}
                >
                  <AccountAvatar login={i.account_login} url={i.avatar_url} />
                  <span className="min-w-0 flex-1 truncate font-medium">{i.account_login}</span>
                  <span className="text-xs text-muted-foreground">
                    {i.account_type === "Organization" ? "Organization" : "Personal"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        <div>{configure}</div>
      </div>
      {go.isError && (
        <Alert variant="destructive" className="m-5 mb-0 w-auto">
          <AlertDescription>{authMessage(go.error)}</AlertDescription>
        </Alert>
      )}
      {current ? (
        <Repos key={current.id} installation={current} onInstallMore={() => go.mutate(true)} />
      ) : (
        <Footer count={0} onImport={null} />
      )}
    </div>
  );
}
