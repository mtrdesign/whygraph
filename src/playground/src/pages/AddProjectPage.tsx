import { useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { portalKey, projectKey, type AddProjectResult } from "../api";
import { saveDetected } from "../lib/detected";
import { GithubSource } from "../components/portal/GithubSource";
import { LocalSource } from "../components/portal/LocalSource";
import { WizardSteps } from "../components/portal/WizardSteps";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";

/**
 * Screens 3-4, step 1 of the add wizard at `/projects/new`: a full page, not a
 * modal. Adding registers the project only (nothing is written into the repo);
 * the wizard then continues at `/p/<slug>/init?step=configure`.
 */
export function AddProjectPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const onAdded = (result: AddProjectResult) => {
    const { slug, name } = result.project;
    saveDetected(slug, result.detected);
    queryClient.setQueryData(projectKey(slug, "project"), result.project);
    void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
    toast.success(`Added ${name}`);
    void navigate({ to: "/p/$slug/init", params: { slug }, search: { step: "configure" } });
  };

  return (
    <div className="mx-auto flex w-full max-w-[760px] flex-col gap-5 p-6 sm:p-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">New project</h1>
        <p className="text-[13px] text-muted-foreground">
          Point WhyGraph at a repository. You can link it to GitHub for pull requests and issues.
        </p>
      </div>
      <WizardSteps current="source" />
      <div className="rounded-xl border border-border bg-card">
        <Tabs defaultValue="local">
          <TabsList variant="line" className="w-full justify-start border-b border-border px-4">
            <TabsTrigger value="local" className="flex-none px-3">
              Local repository
            </TabsTrigger>
            <TabsTrigger value="github" className="flex-none px-3">
              From GitHub
            </TabsTrigger>
          </TabsList>
          <TabsContent value="local">
            <LocalSource onAdded={onAdded} />
          </TabsContent>
          <TabsContent value="github">
            <GithubSource onAdded={onAdded} />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
