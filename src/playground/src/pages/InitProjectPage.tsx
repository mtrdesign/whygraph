import { useEffect } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { portalApi, projectKey } from "../api";
import { isProduction, usePortalState } from "../lib/identity";
import { can } from "../lib/permissions";
import { useSlug } from "../lib/project";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { ConfigForm } from "../components/portal/ConfigForm";
import { FirstScanStep } from "../components/portal/FirstScanStep";
import { InitializeStep } from "../components/portal/InitializeStep";
import { WizardSteps, type WizardStep } from "../components/portal/WizardSteps";
import { Button } from "../components/ui/button";
import type { InitSearch, InitStep } from "../lib/routeSearch";

export type { InitStep };

/** The screen a step shows until S17b's wizard order (the stepper's ids). */
type Screen = Exclude<WizardStep, "source">;

/**
 * Steps 2-4 of the add wizard for an existing project, at `/p/<slug>/init`. The
 * step lives in `?step=setup|configure` (the router redirects the old `initialize`
 * / `scan`), so each is reachable on its own and a project that was added but not
 * initialized resumes here: with no `step`, an uninitialized project lands on Set
 * up and an initialized one on the first scan.
 *
 * Until the wizard's new order lands (M2f-3 S17b), the screens are the old ones:
 * locally `configure` before Initialize is the settings form and `configure` after
 * it the first scan (with the settings below it); `setup` is Initialize. In
 * production `configure` is the first scan plus the settings: the import ran the
 * Initialize already (no agent picker, file preview or hooks on a server copy),
 * so `?step=setup` goes to Configure.
 */
export function InitProjectPage() {
  const slug = useSlug();
  const navigate = useNavigate();
  const search = useSearch({ from: "/p/$slug/init", shouldThrow: false }) as InitSearch | undefined;
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });

  const production = isProduction(usePortalState().data);
  const linked = project.data?.source === "platform";
  const initialized = !!project.data?.initialized;
  // The screen a step shows (see above): before Initialize, a local `configure` is the settings form.
  const screen: Screen | undefined =
    search?.step === "setup"
      ? "initialize"
      : search?.step === "configure"
        ? production || (!linked && !initialized)
          ? "configure"
          : "scan"
        : undefined;
  // Production has no Set up; a linked project has no settings form, so it sets up first.
  const misplaced = (production && screen === "initialize") || (linked && !initialized && screen === "scan");
  useEffect(() => {
    if ((search?.step && !misplaced) || !project.data) return;
    void navigate({
      to: "/p/$slug/init",
      params: { slug },
      search: { step: production || project.data.initialized ? "configure" : "setup" },
      replace: true,
    });
  }, [search?.step, misplaced, production, project.data, navigate, slug]);

  const go = (next: InitStep) =>
    void navigate({ to: "/p/$slug/init", params: { slug }, search: { step: next } });

  if (!screen || misplaced || !project.data) return <p className="p-6 text-sm text-muted-foreground">Loading…</p>;
  const step = screen;

  const titles: Record<Screen, { title: string; blurb: string }> = {
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
        : linked
          ? "Index the code structure on this machine so your agent can place your changes. The history is on the platform."
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
      <WizardSteps current={step} slug={slug} production={production} linked={linked} />

      {step === "configure" && production && <FirstScanStep slug={slug} />}
      {step === "configure" && (
        <ConfigForm
          scope={{ kind: "project", slug }}
          submitLabel={production ? "Save" : "Save and continue"}
          readOnly={!can(project.data, "project.configure")}
          onSaved={production ? undefined : () => go("setup")}
          secondaryActions={
            production ? undefined : (
              <Button variant="ghost" render={<Link to="/" />}>
                Finish later
              </Button>
            )
          }
        />
      )}
      {step === "initialize" &&
        (can(project.data, "project.setup") ? (
          <InitializeStep slug={slug} onDone={() => go("configure")} />
        ) : (
          <Alert data-testid="setup-needs-admin">
            <AlertTitle>A project admin sets this project up</AlertTitle>
            <AlertDescription>Your role cannot initialize the repository or its agent files.</AlertDescription>
          </Alert>
        ))}
      {step === "scan" && <FirstScanStep slug={slug} />}
      {step === "scan" && !linked && (
        <ConfigForm
          scope={{ kind: "project", slug }}
          readOnly={!can(project.data, "project.configure")}
        />
      )}
    </div>
  );
}
