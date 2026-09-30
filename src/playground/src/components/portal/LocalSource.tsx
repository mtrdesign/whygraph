import { useDeferredValue, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2Icon, GitBranchIcon } from "lucide-react";
import {
  portalApi,
  portalKey,
  type AddProjectResult,
  type CheckPathResult,
  type RepoEntry,
} from "../../api";
import { addProjectError, type AddError } from "../../lib/errors";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Field } from "./Field";
import { NotSharedAlert } from "./NotSharedAlert";

function RepoRow({
  repo,
  selected,
  onSelect,
}: {
  repo: RepoEntry;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <label
      className={
        "flex items-center gap-3 border-b border-border px-3.5 py-2.5 last:border-b-0 " +
        (repo.registered ? "cursor-not-allowed text-muted-foreground" : "cursor-pointer hover:bg-muted/50")
      }
    >
      <input
        type="radio"
        name="repo"
        className="accent-primary"
        checked={selected}
        disabled={repo.registered}
        onChange={onSelect}
      />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="font-medium">{repo.name}</span>
        <span className="truncate font-mono text-xs text-muted-foreground" title={repo.path}>
          {repo.path}
        </span>
      </span>
      {repo.registered && <span className="text-xs text-muted-foreground">Already added</span>}
    </label>
  );
}

/**
 * Screen 3: pick a repo discovered under the shared folders, or type a path. A
 * typed (or picked) path is checked first, so an unshared folder shows the
 * `--add-folder` alert and a GitHub-linked repo offers the optional token field.
 */
export function LocalSource({ onAdded }: { onAdded: (result: AddProjectResult) => void }) {
  const [search, setSearch] = useState("");
  const deferred = useDeferredValue(search);
  const [path, setPath] = useState("");
  const [check, setCheck] = useState<CheckPathResult | null>(null);
  const [token, setToken] = useState("");
  const [error, setError] = useState<AddError | null>(null);

  const repos = useQuery({
    queryKey: portalKey("repos", deferred),
    queryFn: () => portalApi.repos(deferred),
  });

  const checkPath = useMutation({
    mutationFn: (p: string) => portalApi.checkPath(p),
    onSuccess: (result) => {
      setCheck(result);
      setError(null);
    },
    onError: (err) => {
      setCheck(null);
      setError(addProjectError(err, "local"));
    },
  });

  const add = useMutation({
    mutationFn: () =>
      portalApi.addProject({
        source: "local",
        path: check!.path,
        ...(token.trim() ? { token: token.trim() } : {}),
      }),
    onSuccess: onAdded,
    onError: (err) => setError(addProjectError(err, "local")),
  });

  const pick = (p: string) => {
    setPath(p);
    setCheck(null);
    setError(null);
    checkPath.mutate(p);
  };

  const ok = check && check.shared && check.is_git && !check.protected;
  const notShared = check && !check.shared;
  const pathError =
    error?.field === "path" && error.message
      ? error
      : check && check.shared && !check.is_git
        ? ({ field: "path", message: "This folder is not a git repository." } as AddError)
        : check?.protected
          ? ({ field: "path", message: "This path overlaps the portal's own data folder." } as AddError)
          : null;

  return (
    <div className="flex flex-col gap-4 p-5">
      <Field label="Repositories in your shared folders">
        {(p) => (
          <Input
            {...p}
            type="search"
            placeholder="Search by name"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        )}
      </Field>

      <div className="flex flex-col rounded-lg border border-border" role="radiogroup" aria-label="Repository">
        {repos.isLoading && <p className="p-3.5 text-sm text-muted-foreground">Looking for repositories…</p>}
        {repos.isError && <p className="p-3.5 text-sm text-destructive">{repos.error.message}</p>}
        {repos.data?.repos.length === 0 && (
          <p className="p-3.5 text-sm text-muted-foreground">
            {deferred
              ? "No repository matches."
              : "No git repositories found in the shared folders. Type a path below."}
          </p>
        )}
        {repos.data?.repos.map((r) => (
          <RepoRow key={r.path} repo={r} selected={path === r.path} onSelect={() => pick(r.path)} />
        ))}
        {repos.data?.truncated && (
          <p className="border-t border-border p-2.5 text-xs text-muted-foreground">
            Showing the first repositories found; search to narrow the list.
          </p>
        )}
      </div>

      <form
        className="flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (path.trim()) pick(path.trim());
        }}
      >
        <Field
          className="flex-1"
          label="Or enter a path"
          error={pathError?.field === "path" && !notShared ? pathError.message : undefined}
        >
          {(p) => (
            <Input
              {...p}
              placeholder="/Users/you/Work/my-repo"
              className="font-mono"
              value={path}
              onChange={(e) => {
                setPath(e.target.value);
                setCheck(null);
                setError(null);
              }}
            />
          )}
        </Field>
        <Button type="submit" variant="outline" disabled={!path.trim() || checkPath.isPending}>
          {checkPath.isPending ? "Checking…" : "Check"}
        </Button>
      </form>

      {(notShared || error?.command) && (
        <NotSharedAlert
          command={check?.command ?? error?.command ?? null}
          checking={checkPath.isPending}
          onCheckAgain={() => checkPath.mutate(check?.path ?? path)}
        />
      )}

      {ok && (
        <div className="flex flex-col gap-3">
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2Icon className="size-4 text-success" />
            Ready to add <span className="font-mono text-xs text-muted-foreground">{check.path}</span>
          </p>
          {check.github && (
            <Field
              label={
                <span className="flex items-center gap-1.5">
                  <GitBranchIcon className="size-3.5" />
                  Linked to github.com/{check.github.slug}
                </span>
              }
              hint="A token is optional: it is only needed to fetch pull requests and issues. It is stored encrypted and never shown again."
              error={error?.field === "token" ? error.message : undefined}
            >
              {(p) => (
                <Input
                  {...p}
                  type="password"
                  autoComplete="off"
                  placeholder="GitHub token (optional)"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
              )}
            </Field>
          )}
        </div>
      )}

      {error && error.field !== "path" && !(error.field === "token" && check?.github) && (
        <Alert variant="destructive">
          <AlertTitle>Could not add the project</AlertTitle>
          <AlertDescription>{error.message}</AlertDescription>
        </Alert>
      )}

      <div className="flex items-center justify-end gap-2 pt-1">
        <Button variant="ghost" render={<Link to="/" />}>
          Cancel
        </Button>
        <Button type="button" disabled={!ok || add.isPending} onClick={() => add.mutate()}>
          {add.isPending ? "Adding…" : "Add project"}
        </Button>
      </div>
    </div>
  );
}
