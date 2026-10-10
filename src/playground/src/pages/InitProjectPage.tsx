import { PageContainer } from "../components/layout/PageContainer";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { portalApi, projectApi, projectKey, type InitResult } from "../api";
import { isProduction, usePortalState } from "../lib/identity";
import { can } from "../lib/permissions";
import { useSlug } from "../lib/project";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { PageSkeleton } from "../components/state/Skeletons";
import { InitializeStep } from "../components/portal/InitializeStep";
import { WizardConfigure } from "../components/portal/WizardConfigure";
import { WIZARD_STEPS, WizardSteps, type WizardMode } from "../components/portal/WizardSteps";
import type { InitSearch, InitStep } from "../lib/routeSearch";

export type { InitStep };

const TITLE: Record<InitStep, string> = { setup: "Set up", configure: "Configure" };

/**
 * The add wizard after Source, at `/p/<slug>/init?step=setup|configure&run=<id>`
 * (M2f-3 plan section 4.9, R2). A local folder is set up first (Initialize: the
 * agents, `.gitignore`, hooks; the first scan is queued by that call), then
 * configured while the scan runs; a production import goes straight to Configure
 * (the server copy needs no Set up); a linked project ends after Set up (the
 * platform owns its configuration). `?run=` is the scan Configure follows, so a
 * reload re-attaches; without it the page follows the running scan, else the
 * newest one. A step that does not belong to the project's wizard redirects to
 * the one that does, and so does a missing `step`.
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
  const p = project.data;
  const mode: WizardMode = production ? "production" : p?.source === "platform" ? "linked" : "local";
  const steps = WIZARD_STEPS[mode];
  const fallback: InitStep = mode === "production" ? "configure" : mode === "linked" || !p?.initialized ? "setup" : "configure";
  const step = search?.step;
  // Configure follows the first scan, which Set up starts: a local project sets up first.
  const misplaced =
    !!step && (!steps.includes(step) || (mode === "local" && step === "configure" && !p?.initialized && !p?.importing));
  useEffect(() => {
    if (!p || (step && !misplaced)) return;
    void navigate({ to: "/p/$slug/init", params: { slug }, search: { step: fallback }, replace: true });
  }, [p, step, misplaced, fallback, navigate, slug]);

  // The run Configure follows: the address, else the running one, else the newest.
  const runParam = search?.run ?? p?.running_scan?.id ?? null;
  const lookUp = step === "configure" && runParam === null && !!p && (p.initialized || !!p.importing);
  const latest = useQuery({
    queryKey: projectKey(slug, "scans", "latest"),
    queryFn: () => projectApi(slug).scans({ limit: 1 }),
    enabled: lookUp,
    retry: false,
  });
  // `undefined` while the newest run is still being looked up.
  const runId: number | null | undefined =
    runParam ?? (lookUp && latest.isPending ? undefined : (latest.data?.runs[0]?.id ?? null));
  // Pin the run into the address, so the card stays on it when it stops running.
  useEffect(() => {
    if (step !== "configure" || search?.run || runId === null || runId === undefined) return;
    void navigate({ to: "/p/$slug/init", params: { slug }, search: { step, run: runId }, replace: true });
  }, [step, search?.run, runId, navigate, slug]);
  const [scanned, setScanned] = useState(false);
  const onFinished = useCallback((done: boolean) => setScanned(done), []);

  const go = (next: InitStep, run?: number | null) =>
    void navigate({
      to: "/p/$slug/init",
      params: { slug },
      search: { step: next, ...(run ? { run } : {}) },
    });

  if (!step || misplaced || !p) return <PageSkeleton label="Loading the wizard" width="narrow" className="sm:py-8" />;

  const blurb =
    step === "setup"
      ? mode === "linked"
        ? "Connect your coding agents to the linked project. Review each file before anything is written."
        : "Connect your coding agents and set the repository up. Review each file before anything is written; the first scan starts right after."
      : scanned
        ? production
          ? "WhyGraph has copied and scanned the repository. Check what commit descriptions need, then open the project. You can change all of this later in Settings."
          : "The first scan is complete. Check what commit descriptions need, then open the project. You can change all of this later in Settings."
        : production
          ? "WhyGraph copies and scans the repository in the background. Meanwhile, check what commit descriptions need. You can change all of this later in Settings."
          : "The first scan runs in the background. Meanwhile, check what commit descriptions need. You can change all of this later in Settings.";

  return (
    <PageContainer width="narrow" className="flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight break-words">
          {p.name}: {TITLE[step]}
        </h1>
        <p className="text-[13px] text-muted-foreground">{blurb}</p>
      </div>
      <WizardSteps current={step} mode={mode} slug={slug} complete={step === "configure" && scanned} />

      {step === "setup" &&
        (can(p, "project.setup") ? (
          <InitializeStep
            slug={slug}
            onDone={(result?: InitResult) => go("configure", result?.initial_run_id)}
          />
        ) : (
          <Alert data-testid="setup-needs-admin">
            <AlertTitle>A project admin sets this project up</AlertTitle>
            <AlertDescription>Your role cannot set up the repository or its agent files.</AlertDescription>
          </Alert>
        ))}
      {step === "configure" && (
        <WizardConfigure
          slug={slug}
          project={p}
          runId={runId}
          onRun={(run) => go("configure", run)}
          onFinished={onFinished}
        />
      )}
    </PageContainer>
  );
}
