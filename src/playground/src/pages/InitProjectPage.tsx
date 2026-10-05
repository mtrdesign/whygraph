import { useEffect } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { portalApi, projectKey } from "../api";
import { isProduction, usePortalState } from "../lib/identity";
import { useSlug } from "../lib/project";
import { ConfigForm } from "../components/portal/ConfigForm";
import { FirstScanStep } from "../components/portal/FirstScanStep";
import { InitializeStep } from "../components/portal/InitializeStep";
import { WizardSteps, type WizardStep } from "../components/portal/WizardSteps";
import { Button } from "../components/ui/button";

export type InitStep = "configure" | "initialize" | "scan";

/**
 * Steps 2-4 of the add wizard for an existing project, at `/p/<slug>/init`. The
 * step lives in `?step=`, so each is reachable on its own and a project that was
 * added but not initialized resumes here: with no `step`, an uninitialized project
 * lands on Initialize and an initialized one on the first scan.
 *
 * In production the steps are Source -> Configure -> First scan: the import ran
 * the Initialize already (no agent picker, file preview or hooks on a server
 * copy), so `?step=initialize` goes to the first scan.
 */
export function InitProjectPage() {
  const slug = useSlug();
  const navigate = useNavigate();
  const search = useSearch({ from: "/p/$slug/init", shouldThrow: false }) as
    | { step?: InitStep }
    | undefined;
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });

  const production = isProduction(usePortalState().data);
  const step = search?.step;
  const misplaced = production && step === "initialize";
  useEffect(() => {
    if ((step && !misplaced) || !project.data) return;
    void navigate({
      to: "/p/$slug/init",
      params: { slug },
      search: { step: production || project.data.initialized ? "scan" : "initialize" },
      replace: true,
    });
  }, [step, misplaced, production, project.data, navigate, slug]);

  const go = (next: InitStep) =>
    void navigate({ to: "/p/$slug/init", params: { slug }, search: { step: next } });

  if (!step || misplaced) return <p className="p-6 text-sm text-muted-foreground">Loading…</p>;

  const titles: Record<InitStep, { title: string; blurb: string }> = {
    configure: {
      title: "Configure",
      blurb: "Choose the models and keys WhyGraph uses for this project. You can change all of this later in Settings.",
    },
    initialize: {
      title: "Initialize",
      blurb: "Connect your coding agents and set up the repository. Review each file before anything is written.",
    },
    scan: {
      title: "First scan",
      blurb: production
        ? "Index the repository so the Explorer and Chat have something to read."
        : "Index the repository so the Explorer, Chat and your agents have something to read.",
    },
  };

  return (
    <div className="mx-auto flex w-full max-w-[760px] flex-col gap-5 p-6 sm:p-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">
          {project.data?.name ?? slug}: {titles[step].title}
        </h1>
        <p className="text-[13px] text-muted-foreground">{titles[step].blurb}</p>
      </div>
      <WizardSteps current={step as WizardStep} slug={slug} production={production} />

      {step === "configure" && (
        <ConfigForm
          scope={{ kind: "project", slug }}
          submitLabel="Save and continue"
          onSaved={() => go(production ? "scan" : "initialize")}
          secondaryActions={
            <Button variant="ghost" render={<Link to="/" />}>
              Finish later
            </Button>
          }
        />
      )}
      {step === "initialize" && <InitializeStep slug={slug} onDone={() => go("scan")} />}
      {step === "scan" && <FirstScanStep slug={slug} />}
    </div>
  );
}
