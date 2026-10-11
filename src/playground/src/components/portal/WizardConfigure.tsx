import { useEffect, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2Icon, InfoIcon } from "lucide-react";
import {
  ApiError,
  portalApi,
  portalKey,
  projectApi,
  projectKey,
  type ConfigDict,
  type ProjectDetails,
  type ScanEstimate,
  type ScanRunRow,
} from "../../api";
import { layerToValues, type ModelPick } from "../../lib/configForm";
import { canOwn, isProduction, usePortalState, useReadOnly, useRole } from "../../lib/identity";
import { inheritedLayerLabel, providerLabel } from "../../lib/labels";
import { can } from "../../lib/permissions";
import { plural } from "../../lib/plural";
import { scanAvailability } from "../../lib/scanAvailability";
import { costPhrase, modelLabel } from "../../lib/scanFormat";
import { useScanRun, type ScanRunState } from "../../lib/scanRun";
import { scanRunKey } from "../shell/crumbs";
import { DisabledReason } from "../state/DisabledReason";
import { ErrorState } from "../state/ErrorState";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Skeleton } from "../ui/skeleton";
import { Field } from "./Field";
import { ScanEstimateCard } from "./ScanEstimateCard";
import { ScanProgress } from "./ScanProgress";

const CARD = "flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card";

/** What a run is, from its row (BUG-22: not from local state, so a reload keeps it). */
type RunKind = "initial" | "describe" | "other";

function runKind(row: ScanRunRow | undefined): RunKind {
  if (!row) return "initial";
  if (row.trigger === "describe" || row.analyze) return "describe";
  return row.trigger === "initial" || row.trigger === "manual" ? "initial" : "other";
}

const TITLES: Record<RunKind, { running: string; ok: string }> = {
  initial: { running: "First scan", ok: "First scan complete" },
  describe: { running: "Writing descriptions", ok: "Descriptions written" },
  other: { running: "Scan", ok: "Scan complete" },
};

/**
 * One run as the wizard follows it: the events stream folded into
 * `ScanRunState`, the run's row (its kind and title, cached under the run page's
 * key) and, when it ends, a refresh of what the run changed.
 */
export function useWizardRun(slug: string, runId: number | null) {
  const queryClient = useQueryClient();
  const state = useScanRun(slug, runId);
  const row = useQuery({
    queryKey: scanRunKey(slug, runId ?? 0),
    queryFn: () => projectApi(slug).scan(runId!),
    enabled: runId !== null,
    retry: false,
  });
  useEffect(() => {
    if (!state.finished) return;
    void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
    void queryClient.invalidateQueries({ queryKey: projectKey(slug, "scan-estimate") });
    void queryClient.invalidateQueries({ queryKey: projectKey(slug, "config") });
    if (runId !== null) void queryClient.invalidateQueries({ queryKey: scanRunKey(slug, runId) });
  }, [state.finished, queryClient, slug, runId]);
  return { state, row: row.data, kind: runKind(row.data) };
}

/**
 * The first scan's progress card: one bar, the status line and the checklist
 * while it runs; "First scan complete" (or "Descriptions written") when it ends
 * well; the reason and **Try again** when it does not. `onRetried` gets the new
 * run's id.
 */
export function RunCardView({
  slug,
  runId,
  state,
  kind,
  fullName,
  project,
  onRetried,
}: {
  slug: string;
  runId: number;
  state: ScanRunState;
  kind: RunKind;
  fullName?: string;
  project?: ProjectDetails;
  onRetried?: (runId: number) => void;
}) {
  const retry = useMutation({
    mutationFn: () =>
      projectApi(slug).requestScan(kind === "describe" ? { trigger: "describe" } : { trigger: "manual", analyze: false }),
    onSuccess: ({ run_id }) => onRetried?.(run_id),
  });
  const title = TITLES[kind];

  if (state.failure) {
    const error = new ApiError(state.failure.status, state.failure.message, state.failure.code);
    return (
      <section className={CARD} data-testid="run-card">
        <h2 className="text-sm font-semibold">{title.running}</h2>
        <ErrorState error={error} title="Couldn't follow the scan" />
      </section>
    );
  }

  if (state.finished && state.finished !== "ok") {
    const message =
      state.error ??
      (typeof state.summary?.error === "string" ? state.summary.error : null) ??
      `The scan ${state.finished === "failed" ? "failed" : `was ${state.finished}`}.`;
    const avail = project ? scanAvailability(project) : null;
    // Retrying a describe run spends tokens, so it needs what a full scan needs.
    const mayRetry =
      !!onRetried && !!avail && (kind === "describe" ? avail.full.allowed : avail.quick.allowed || avail.retryImport);
    return (
      <section className={CARD} data-testid="run-card">
        <h2 className="text-sm font-semibold">The scan did not finish</h2>
        <Alert variant="destructive" data-testid="scan-failed">
          <AlertTitle>{state.finished === "cancelled" ? "Scan cancelled" : "Scan failed"}</AlertTitle>
          <AlertDescription>{message}</AlertDescription>
        </Alert>
        <ScanProgress slug={slug} runId={runId} state={state} fullName={fullName} />
        {mayRetry && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={() => retry.mutate()} disabled={retry.isPending}>
              {retry.isPending ? "Starting…" : "Try again"}
            </Button>
            {retry.isError && <ErrorState error={retry.error} size="inline" />}
          </div>
        )}
      </section>
    );
  }

  return (
    <section className={CARD} data-testid="run-card" data-status={state.finished ?? "running"}>
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        {state.finished === "ok" && <CheckCircle2Icon className="size-4 text-success" />}
        {state.finished === "ok" ? title.ok : title.running}
      </h2>
      {state.finished === "ok" && kind === "initial" && (
        <p className="text-sm text-muted-foreground">
          {project?.source === "platform"
            ? "The code structure is indexed. Your agent now gets the platform's history for this project, placed against your own checkout."
            : "Git history and code structure are indexed. You can open the project now."}
        </p>
      )}
      <ScanProgress slug={slug} runId={runId} state={state} fullName={fullName} />
    </section>
  );
}

/** `RunCardView` for one run id, following it on its own (the linked project's done panel). */
export function RunCard({ slug, runId }: { slug: string; runId: number }) {
  const project = useQuery({ queryKey: projectKey(slug, "project"), queryFn: () => portalApi.project(slug) });
  const [current, setCurrent] = useState(runId);
  const run = useWizardRun(slug, current);
  return (
    <RunCardView
      slug={slug}
      runId={current}
      state={run.state}
      kind={run.kind}
      project={project.data}
      onRetried={setCurrent}
    />
  );
}

/**
 * The model commit descriptions use, as far as the client can tell: the project
 * layer's `[analyze]`, then its default model, then the inherited layer's, then
 * WhyGraph's default provider. The scan estimate (the server's own resolution)
 * wins once it is in; whether the pick is inherited still follows the layers (a
 * project layer with no model of its own inherits whatever the estimate names).
 */
export function describeModel(
  project: ConfigDict | undefined,
  inherited: ConfigDict | undefined,
  estimate?: ScanEstimate,
): ModelPick & { inherited: boolean } {
  const pick = (layer: ConfigDict | undefined): ModelPick | null => {
    if (!layer) return null;
    const v = layerToValues(layer);
    if (v.analyze.provider) return v.analyze;
    if (v.defaultModel.provider) return v.defaultModel;
    return null;
  };
  const own = pick(project);
  if (estimate?.model.provider) {
    // Unknown until the project layer is in: say nothing rather than guess.
    return { provider: estimate.model.provider, model: estimate.model.model ?? "", inherited: !!project && !own };
  }
  if (own) return { ...own, inherited: false };
  const up = pick(inherited);
  if (up) return { ...up, inherited: true };
  return { provider: "anthropic", model: "", inherited: true };
}

/** "What descriptions need": the describe model and whether its key is in place (IMP-6). */
function DescriptionsNeed({
  slug,
  project,
  config,
  inherited,
  estimate,
}: {
  slug: string;
  project: ProjectDetails;
  config: { config: ConfigDict; effective_keys?: Record<string, string> } | undefined;
  inherited: ConfigDict | undefined;
  estimate: ScanEstimate | undefined;
}) {
  const queryClient = useQueryClient();
  const portal = usePortalState().data;
  const production = isProduction(portal);
  const readOnly = useReadOnly();
  const mayConfigure = can(project, "project.configure") && !readOnly;
  // Only an owner changes the org's keys: they get the link, not "an owner can add one".
  const owner = canOwn(useRole());
  const [key, setKey] = useState("");
  const model = describeModel(config?.config, inherited, estimate);
  const provider = model.provider;
  const name = providerLabel(provider);
  const keyless = provider === "ollama";
  const scope = config?.effective_keys?.[provider] ?? (estimate?.missing_key === provider ? "none" : undefined);
  const missing = !keyless && (scope === "none" || estimate?.missing_key === provider);
  const layer = inheritedLayerLabel(portal?.mode);
  const save = useMutation({
    mutationFn: () => projectApi(slug).putConfig({ secrets: { llm: { [provider]: key.trim() } } }),
    onSuccess: () => {
      setKey("");
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "config") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "scan-estimate") });
    },
  });

  if (!config && !estimate) {
    return (
      <section className={CARD} data-testid="descriptions-need">
        <h2 className="text-sm font-semibold">What descriptions need</h2>
        <Skeleton className="h-10" />
      </section>
    );
  }
  return (
    <section className={CARD} data-testid="descriptions-need">
      <div>
        <h2 className="text-sm font-semibold">What descriptions need</h2>
        <p className="text-xs text-muted-foreground">
          Describing commits is the only part that calls an LLM. It can wait: the Explorer and Chat work
          without it.
        </p>
      </div>
      <p className="text-sm" data-testid="describe-model">
        {/* One form while the run goes and after it (never a raw provider/model id). */}
        Model: {modelLabel(name, model.model)}
        {model.inherited && <span className="text-muted-foreground"> (inherited from {layer})</span>}
      </p>
      {!missing ? (
        <p className="flex items-center gap-2 text-sm" data-testid="key-ready">
          <CheckCircle2Icon className="size-4 text-success" />
          {keyless
            ? `${name} needs no key.`
            : scope === "project"
              ? `Uses this project's ${name} key.`
              : scope === "environment"
                ? `Uses the ${name} key from the portal's environment.`
                : `Uses the ${name} key from ${layer}.`}
        </p>
      ) : mayConfigure ? (
        <Alert variant="info" data-testid="key-missing">
          <InfoIcon />
          <AlertTitle>No {name} key yet</AlertTitle>
          <AlertDescription>
            <p>Descriptions can wait; add one now or later.</p>
            <form
              className="mt-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (key.trim()) save.mutate();
              }}
            >
              {/* The button sits beside the input and the hint runs under both, on every width. */}
              <Field
                label={`${name} API key`}
                hint={
                  !production ? (
                    "Stored encrypted. Only its last characters are ever shown."
                  ) : owner ? (
                    <>
                      Saved for this project only. To use one key for every project, add it in{" "}
                      <Link to="/settings" search={{ section: "models" }} className="text-primary-text hover:underline">
                        Organization settings
                      </Link>
                      .
                    </>
                  ) : (
                    "Saved for this project only. An organization owner can add one for every project."
                  )
                }
              >
                {(p) => (
                  <div className="flex gap-2">
                    <Input
                      {...p}
                      className="min-w-0 flex-1"
                      type="password"
                      autoComplete="off"
                      value={key}
                      onChange={(e) => setKey(e.target.value)}
                    />
                    <Button type="submit" className="shrink-0" disabled={!key.trim() || save.isPending}>
                      {save.isPending ? "Saving…" : "Save key"}
                    </Button>
                  </div>
                )}
              </Field>
            </form>
            {save.isError && <ErrorState error={save.error} size="inline" className="mt-2" />}
          </AlertDescription>
        </Alert>
      ) : (
        <Alert variant="info" data-testid="key-missing">
          <InfoIcon />
          <AlertDescription>
            {production ? (
              <p>
                Your organization has no {name} key. An owner can add one in{" "}
                <Link to="/settings" search={{ section: "models" }} className="text-primary-text hover:underline">
                  Organization settings
                </Link>
                .
              </p>
            ) : (
              <p>No {name} key yet. Descriptions can wait.</p>
            )}
          </AlertDescription>
        </Alert>
      )}
    </section>
  );
}

/**
 * The local GitHub token row (R2): a project whose remote is on GitHub and that
 * has no token in effect. Saving the token also asks for a quick rescan, which
 * the runner merges with the running first scan or queues behind it, so pull
 * requests and issues arrive without a full rescan. The row follows that rescan
 * (a link to its run page) and refreshes the project when it ends, so the
 * Overview never inherits a stale "Scanning".
 */
function GitHubTokenRow({
  slug,
  project,
  github,
  firstScanDone,
  runId,
}: {
  slug: string;
  project: ProjectDetails;
  github: { remote: string | null; token: "project" | "org" | "none" };
  /** The first scan has finished: the token is used by a rescan of its own now. */
  firstScanDone: boolean;
  /** The run the page follows (the first scan). */
  runId: number | null;
}) {
  const queryClient = useQueryClient();
  const readOnly = useReadOnly();
  const [token, setToken] = useState("");
  const [rescan, setRescan] = useState<number | null>(null);
  // Folded only for its end: useWizardRun refreshes the project then.
  const followed = useWizardRun(slug, rescan !== null && rescan !== runId ? rescan : null);
  const save = useMutation({
    mutationFn: async () => {
      await projectApi(slug).putConfig({ secrets: { github_token: token.trim() } });
      return projectApi(slug).requestScan({ trigger: "manual", analyze: false });
    },
    onSuccess: ({ run_id }) => {
      setToken("");
      setRescan(run_id);
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "config") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    },
  });
  const saved = rescan !== null;
  const rescanEnded = followed.state.finished;
  if (!github.remote) return null;
  return (
    <section className={CARD} data-testid="github-token-row">
      <div>
        <h2 className="text-sm font-semibold">GitHub pull requests and issues</h2>
        <p className="text-xs text-muted-foreground">
          The repository is <span className="font-mono">github.com/{github.remote}</span>.
        </p>
      </div>
      {saved ? (
        <p className="row-wrap items-center gap-x-2 gap-y-1 text-sm" data-testid="github-token-saved">
          <CheckCircle2Icon className="size-4 shrink-0 text-success" />
          <span>
            {!firstScanDone
              ? "Saved. WhyGraph fetches pull requests and issues right after the first scan."
              : rescanEnded === "ok"
                ? "Saved. The rescan has fetched pull requests and issues."
                : rescanEnded
                  ? "Saved. The rescan did not finish."
                  : "Saved. WhyGraph fetches pull requests and issues in a quick rescan now."}
          </span>
          {rescan !== null && rescan !== runId && (
            <Link
              to="/p/$slug/scans/{-$runId}"
              params={{ slug, runId: String(rescan) }}
              className="text-primary-text hover:underline"
              data-testid="github-token-rescan"
            >
              {rescanEnded ? "Show the rescan" : "Follow the rescan"}
            </Link>
          )}
        </p>
      ) : github.token === "org" ? (
        <p className="text-sm text-muted-foreground">Using the portal default token. Nothing to do.</p>
      ) : github.token === "project" ? (
        <p className="text-sm text-muted-foreground">This project has its own GitHub token.</p>
      ) : can(project, "project.configure") && !readOnly ? (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (token.trim()) save.mutate();
          }}
        >
          <p className="text-sm">
            {firstScanDone
              ? "PRs and issues need a GitHub token. Add one and WhyGraph fetches them in a quick rescan."
              : "PRs and issues need a GitHub token. Add one and WhyGraph fetches them right after the first scan."}
          </p>
          {/* The button sits beside the input and the hint runs under both, on every width. */}
          <Field label="GitHub token" hint="Read access to the repository is enough. Stored encrypted.">
            {(p) => (
              <div className="flex gap-2">
                <Input
                  {...p}
                  className="min-w-0 flex-1"
                  type="password"
                  autoComplete="off"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
                <Button type="submit" className="shrink-0" disabled={!token.trim() || save.isPending}>
                  {save.isPending ? "Saving…" : "Save token"}
                </Button>
              </div>
            )}
          </Field>
          {save.isError && <ErrorState error={save.error} size="inline" />}
        </form>
      ) : (
        <p className="text-sm text-muted-foreground">
          PRs and issues need a GitHub token. A project admin can add one in the project's settings.
        </p>
      )}
    </section>
  );
}

/**
 * The wizard's Configure step (M2f-3 plan section 4.9, IMP-3..IMP-6, R2): the
 * first scan's progress (the run in `?run=`, so a reload re-attaches), what
 * descriptions need (the model and its key), the GitHub token row (local), a link
 * to the rest of the settings, the describe estimate once the scan has read the
 * history, and a footer with **Open project** and **Describe N commits**.
 * `onRun` moves the page to another run (a retry, or the describe run).
 */
export function WizardConfigure({
  slug,
  project,
  runId,
  onRun,
  onFinished,
}: {
  slug: string;
  project: ProjectDetails;
  /** The run to follow; `null` for none, `undefined` while the page still looks for one. */
  runId: number | null | undefined;
  onRun: (runId: number) => void;
  /** Called with whether the first scan has finished (the stepper ticks Configure then, BUG-14). */
  onFinished?: (finished: boolean) => void;
}) {
  const navigate = useNavigate();
  const portal = usePortalState().data;
  const production = isProduction(portal);
  const run = useWizardRun(slug, runId ?? null);
  // The attached run ended well (with none, the project has a scan): the estimate can load.
  const finished = runId === undefined ? false : runId === null ? !!project.last_scan_at : run.state.finished === "ok";
  // The first scan is done (a describe run only starts after it): the stepper ticks Configure.
  const scanned = finished || (run.kind === "describe" && !!project.last_scan_at);
  const linked = project.source === "platform";
  const config = useQuery({
    queryKey: projectKey(slug, "config"),
    queryFn: () => projectApi(slug).config(),
    retry: false,
  });
  const defaults = useQuery({
    queryKey: portalKey("defaults"),
    queryFn: portalApi.defaults,
    retry: false,
  });
  const estimate = useQuery({
    queryKey: projectKey(slug, "scan-estimate"),
    queryFn: () => projectApi(slug).scanEstimate(),
    enabled: finished && !linked && project.initialized,
    retry: false,
  });
  const describe = useMutation({
    mutationFn: () => projectApi(slug).requestScan({ trigger: "describe" }),
    onSuccess: ({ run_id }) => onRun(run_id),
  });

  useEffect(() => onFinished?.(scanned), [scanned, onFinished]);

  const fullName = project.github_full_name ?? undefined;
  const totalCommits = run.state.summary?.coverage?.commits ?? project.stats?.commits ?? null;
  const avail = scanAvailability(project, { analyzeMissingKey: estimate.data?.missing_key ?? null });
  const waiting = estimate.data?.commits ?? null;
  const describeReason = !finished
    ? run.kind === "describe"
      ? "The descriptions are being written"
      : "Wait for the first scan to finish"
    : !estimate.data
      ? "Estimating the cost"
      : avail.full.allowed
        ? undefined
        : avail.full.reason;
  const cost = estimate.data?.cost && !estimate.data.cost_hidden ? ` (${costPhrase(estimate.data.cost.usd)})` : "";
  const describeLabel = waiting ? `Describe ${plural(waiting, "commit")}${cost}` : "Describe commits";
  const describeButton = (
    <Button variant="outline" onClick={() => describe.mutate()} disabled={!!describeReason || describe.isPending}>
      {describe.isPending ? "Starting…" : describeLabel}
    </Button>
  );

  return (
    <div className="flex flex-col gap-5">
      {runId === undefined ? (
        <Skeleton className="h-28 rounded-xl" />
      ) : runId !== null ? (
        <RunCardView
          slug={slug}
          runId={runId}
          state={run.state}
          kind={run.kind}
          fullName={fullName}
          project={project}
          onRetried={onRun}
        />
      ) : project.last_scan_at ? null : (
        <FirstScanStart slug={slug} project={project} onRun={onRun} />
      )}

      {config.isError ? (
        <ErrorState error={config.error} title="Couldn't load the project's settings" onRetry={() => void config.refetch()} />
      ) : (
        <DescriptionsNeed
          slug={slug}
          project={project}
          config={config.data}
          inherited={defaults.data?.config}
          estimate={estimate.data}
        />
      )}

      {!production && config.data?.github && (
        <GitHubTokenRow
          slug={slug}
          project={project}
          github={config.data.github}
          firstScanDone={scanned}
          runId={runId ?? null}
        />
      )}

      <p className="text-sm">
        <Link to="/p/$slug/settings" params={{ slug }} className="text-primary-text hover:underline">
          {production ? "More settings (chat model, limits)" : "More settings (chat model, hooks, limits)"}
        </Link>
      </p>

      <section className={CARD} data-testid="wizard-estimate">
        <h2 className="text-sm font-semibold">Commit descriptions</h2>
        {finished ? (
          <ScanEstimateCard
            slug={slug}
            actions={false}
            totalCommits={totalCommits}
            onDescribe={() => describe.mutate()}
            onLater={() => void navigate({ to: "/p/$slug", params: { slug } })}
          />
        ) : (
          <p className="text-sm text-muted-foreground" data-testid="estimate-pending">
            {run.kind === "describe"
              ? "Updating once the descriptions are written…"
              : "Estimating once the first scan has read the history…"}
          </p>
        )}
      </section>

      {describe.isError && <ErrorState error={describe.error} title="Couldn't start the descriptions" />}

      <div className="row-wrap items-center justify-end gap-2 border-t border-border pt-4">
        {waiting !== 0 &&
          (describeReason ? <DisabledReason reason={describeReason}>{describeButton}</DisabledReason> : describeButton)}
        <Button onClick={() => void navigate({ to: "/p/$slug", params: { slug } })}>Open project</Button>
      </div>
    </div>
  );
}

/** No run to follow and no scan yet (the queue refused it, or the page lost the run): start it here. */
function FirstScanStart({
  slug,
  project,
  onRun,
}: {
  slug: string;
  project: ProjectDetails;
  onRun: (runId: number) => void;
}) {
  const start = useMutation({
    mutationFn: () => projectApi(slug).requestScan({ trigger: "manual", analyze: false }),
    onSuccess: ({ run_id }) => onRun(run_id),
  });
  const avail = scanAvailability(project);
  return (
    <section className={CARD} data-testid="run-card">
      <h2 className="text-sm font-semibold">First scan</h2>
      <p className="text-sm text-muted-foreground">
        The first scan reads git history and indexes the code structure. It makes no LLM calls, so it
        costs nothing.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => start.mutate()} disabled={start.isPending || !(avail.quick.allowed || avail.retryImport)}>
          {start.isPending ? "Starting…" : "Start the first scan"}
        </Button>
        {start.isError && <ErrorState error={start.error} size="inline" />}
      </div>
    </section>
  );
}
