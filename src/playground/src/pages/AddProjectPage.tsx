import { useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { portalKey, projectKey, type AddProjectResult } from "../api";
import { saveDetected } from "../lib/detected";
import { isProduction, usePortalState } from "../lib/identity";
import { GitHubImport } from "../components/portal/GitHubImport";
import { LocalSource } from "../components/portal/LocalSource";
import { WizardSteps } from "../components/portal/WizardSteps";

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

  const onAdded = (result: AddProjectResult) => {
    const { slug, name } = result.project;
    if (!production) saveDetected(slug, result.detected);
    queryClient.setQueryData(projectKey(slug, "project"), result.project);
    void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
    void queryClient.invalidateQueries({ queryKey: portalKey("github") });
    toast.success(`${production ? "Imported" : "Added"} ${name}`);
    void navigate({ to: "/p/$slug/init", params: { slug }, search: { step: "configure" } });
  };

  return (
    <div className="mx-auto flex w-full max-w-[760px] flex-col gap-5 p-6 sm:p-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">
          {production ? "Import from GitHub" : "New project"}
        </h1>
        <p className="text-[13px] text-muted-foreground">
          {production
            ? "Pick a repository the WhyGraph GitHub App can read. WhyGraph keeps its own copy and scans the default branch."
            : "Point WhyGraph at a repository from a shared folder. You can link it to GitHub for pull requests and issues."}
        </p>
      </div>
      <WizardSteps current="source" production={production} />
      <div className="rounded-xl border border-border bg-card">
        {production ? <GitHubImport onImported={onAdded} /> : <LocalSource onAdded={onAdded} />}
      </div>
    </div>
  );
}
