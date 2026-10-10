import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, githubApi, portalKey, projectKey, type AddProjectResult } from "../api";
import { saveDetected } from "../lib/detected";
import { isProduction, usePortalState } from "../lib/identity";
import { cn } from "@/lib/utils";
import { GitHubImport } from "../components/portal/GitHubImport";
import { LocalSource } from "../components/portal/LocalSource";
import { PlatformSource } from "../components/portal/PlatformSource";
import { WizardSteps } from "../components/portal/WizardSteps";

export interface NewProjectSearch {
  source?: "platform";
  link?: string;
}

/**
 * Step 1 of the add wizard at `/projects/new`: a full page, not a modal, with one
 * title and (local mode) two fixed tabs, "This computer" and "From a platform";
 * only the stepper's length follows the tab (IMP-8). Local mode adds a repository
 * from a shared folder (nothing is written into it) and continues at Set up;
 * a platform link continues at Set up too; production imports from GitHub, and
 * the import page moves on to Configure by itself.
 */
export function AddProjectPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const production = isProduction(usePortalState().data);
  const search = useSearch({ strict: false }) as NewProjectSearch;
  // A pending link only exists on the platform source; local mode only (M2e).
  const platform = !production && (search.source === "platform" || !!search.link);
  // The same request the import page makes (one cache entry): a bootstrap
  // password account cannot import, and then a stepper would promise steps.
  const installations = useQuery({
    queryKey: portalKey("github", "installations"),
    queryFn: githubApi.installations,
    enabled: production,
    retry: false,
  });
  const githubRequired =
    installations.error instanceof ApiError && installations.error.code === "github_required";

  const onAdded = (result: AddProjectResult) => {
    const { slug } = result.project;
    saveDetected(slug, result.detected);
    queryClient.setQueryData(projectKey(slug, "project"), result.project);
    void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
    // No toast: the Set up page names the project (CN-1).
    void navigate({ to: "/p/$slug/init", params: { slug }, search: { step: "setup" } });
  };

  return (
    <div className="mx-auto flex w-full max-w-[760px] flex-col gap-5 p-4 sm:p-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">Add a project</h1>
        <p className="text-[13px] text-muted-foreground">
          {production
            ? "Import repositories the WhyGraph GitHub App can read. WhyGraph keeps its own copy of each and scans the default branch."
            : "Add a repository from a folder shared with the portal, or link a checkout to a project on a WhyGraph platform."}
        </p>
      </div>
      {!production && (
        <nav aria-label="Source" className="flex gap-1 self-start rounded-lg border border-border p-0.5 text-sm">
          {(
            [
              { id: "local", label: "This computer", on: !platform },
              { id: "platform", label: "From a platform", on: platform },
            ] as const
          ).map((t) => (
            <Link
              key={t.id}
              to="/projects/new"
              search={t.id === "platform" ? { source: "platform" } : {}}
              aria-current={t.on ? "page" : undefined}
              className={cn(
                "rounded-md px-3 py-1.5",
                t.on ? "bg-muted font-medium text-foreground" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {t.label}
            </Link>
          ))}
        </nav>
      )}
      {!githubRequired && (
        <WizardSteps current="source" mode={production ? "production" : platform ? "linked" : "local"} />
      )}
      <div className="rounded-xl border border-border bg-card">
        {production ? (
          <GitHubImport />
        ) : platform ? (
          <>
            <p className="px-5 pt-5 text-[13px] text-muted-foreground">
              Connect to a WhyGraph platform, then pick the checkout of its repository on this machine. The
              platform keeps the history; this machine answers your agent about your own changes.
            </p>
            <PlatformSource linkId={search.link} onAdded={onAdded} />
          </>
        ) : (
          <LocalSource onAdded={onAdded} />
        )}
      </div>
    </div>
  );
}
