import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { portalKey, projectKey, type AddProjectResult } from "../api";
import { saveDetected } from "../lib/detected";
import { isProduction, usePortalState } from "../lib/identity";
import { GitHubImport } from "../components/portal/GitHubImport";
import { LocalSource } from "../components/portal/LocalSource";
import { PlatformSource } from "../components/portal/PlatformSource";
import { WizardSteps } from "../components/portal/WizardSteps";

export interface NewProjectSearch {
  source?: "platform";
  link?: string;
}

/**
 * Screens 3-4, step 1 of the add wizard at `/projects/new`: a full page, not a
 * modal. Local mode adds a repository from a shared folder (nothing is written
 * into it); production imports one from GitHub (the server clones and
 * initializes it). The wizard then continues at `/p/<slug>/init?step=configure`.
 */
export function AddProjectPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const production = isProduction(usePortalState().data);
  const search = useSearch({ strict: false }) as NewProjectSearch;
  // A pending link only exists on the platform source; local mode only (M2e).
  const platform = !production && (search.source === "platform" || !!search.link);

  const onAdded = (result: AddProjectResult) => {
    const { slug, name } = result.project;
    if (!production) saveDetected(slug, result.detected);
    queryClient.setQueryData(projectKey(slug, "project"), result.project);
    void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
    void queryClient.invalidateQueries({ queryKey: portalKey("github") });
    toast.success(`${production ? "Imported" : platform ? "Linked" : "Added"} ${name}`);
    // A linked project's config belongs to the platform: Configure is skipped.
    void navigate({ to: "/p/$slug/init", params: { slug }, search: { step: platform ? "setup" : "configure" } });
  };

  return (
    <div className="mx-auto flex w-full max-w-[760px] flex-col gap-5 p-6 sm:p-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">
          {production ? "Import from GitHub" : platform ? "Link a platform project" : "New project"}
        </h1>
        <p className="text-[13px] text-muted-foreground">
          {production
            ? "Pick a repository the WhyGraph GitHub App can read. WhyGraph keeps its own copy and scans the default branch."
            : platform
              ? "Connect to a WhyGraph platform, then pick the checkout of its repository on this machine. The platform keeps the history; this machine answers your agent about your own changes."
              : "Point WhyGraph at a repository from a shared folder. You can link it to GitHub for pull requests and issues."}
        </p>
      </div>
      {!production && (
        <nav aria-label="Source" className="flex gap-1 self-start rounded-lg border border-border p-0.5 text-sm">
          {(
            [
              { id: "local", label: "Local folder", on: !platform },
              { id: "platform", label: "WhyGraph platform", on: platform },
            ] as const
          ).map((t) => (
            <Link
              key={t.id}
              to="/projects/new"
              search={t.id === "platform" ? { source: "platform" } : {}}
              aria-current={t.on ? "page" : undefined}
              className={
                "rounded-md px-3 py-1.5 " +
                (t.on ? "bg-muted font-medium text-foreground" : "text-muted-foreground hover:text-foreground")
              }
            >
              {t.label}
            </Link>
          ))}
        </nav>
      )}
      <WizardSteps current="source" production={production} linked={platform} />
      <div className="rounded-xl border border-border bg-card">
        {production ? (
          <GitHubImport onImported={onAdded} />
        ) : platform ? (
          <PlatformSource linkId={search.link} onAdded={onAdded} />
        ) : (
          <LocalSource onAdded={onAdded} />
        )}
      </div>
    </div>
  );
}
