import { useMemo, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2Icon, InfoIcon, TriangleAlertIcon } from "lucide-react";
import {
  ApiError,
  portalApi,
  portalKey,
  projectApi,
  projectKey,
  type Detected,
  type InitResult,
  type ProjectDetails,
} from "../../api";
import { errorInfo } from "../../lib/apiErrors";
import { loadDetected } from "../../lib/detected";
import { usePortalState } from "../../lib/identity";
import { agentLabel } from "../../lib/labels";
import { cn } from "@/lib/utils";
import { ErrorState } from "../state/ErrorState";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Checkbox } from "../ui/checkbox";
import { AgentPicker } from "./AgentPicker";
import { ConnectAgent } from "./ConnectAgent";
import { CopyButton } from "./CopyButton";
import { DetectedPanel } from "./DetectedPanel";
import { InitPreview } from "./InitPreview";
import { RunCard } from "./WizardConfigure";

const EMPTY: Detected = {
  existing_db: false,
  managed_hooks: [],
  detected_agents: [],
  custom_db_paths: [],
};

/** A card in the wizard; in settings the surrounding section is the card (SET-8). */
function Block({ flat, children, testId }: { flat: boolean; children: React.ReactNode; testId?: string }) {
  return (
    <section
      className={cn("flex flex-col gap-3", !flat && "rounded-xl border border-border bg-card p-5")}
      data-testid={testId}
    >
      {children}
    </section>
  );
}

/**
 * The wizard's Set up step (the internal name stays Initialize: `POST /init`):
 * pick agents, preview exactly which files will change, then set the repository
 * up. A git-tracked agent file needs its confirm box ticked first (deselect its
 * agent to leave it alone).
 *
 * The first, non-dry-run Set up queues the first scan on the server and returns
 * its `initial_run_id`; `onDone` gets the result (the page moves to Configure with
 * the run). A refused file, a hook failure or a refused scan stay on a done panel
 * first: the snippet to paste, the reason, and **Start the first scan**. A linked
 * project ends here, on a done panel with its first scan and "Connect your agent";
 * it preselects the agents the checkout already has and warns when none is ticked.
 */
export function InitializeStep({
  slug,
  onDone,
  mode = "wizard",
  configured = [],
  detected: detectedProp = null,
}: {
  slug: string;
  /** Wizard: the Set up result (with the queued run); settings: after "Done". */
  onDone: (result?: InitResult) => void;
  /**
   * `settings` reuses this step on an initialized project (screen 10): the picker
   * starts from the agents already `configured`, unticking one removes its entry,
   * and "Update agent files" (`force`) rewrites files that already exist. It renders
   * inside the Agents section, without a card of its own, and queues no scan.
   */
  mode?: "wizard" | "settings";
  configured?: readonly string[];
  detected?: Detected | null;
}) {
  const settings = mode === "settings";
  const queryClient = useQueryClient();
  const port = usePortalState().data?.port;
  const project = useQuery({ queryKey: projectKey(slug, "project"), queryFn: () => portalApi.project(slug) });
  const linked = project.data?.source === "platform";
  const detected = useMemo(
    () =>
      settings
        ? detectedProp
        : (loadDetected(slug) ??
          queryClient.getQueryData<ProjectDetails>(projectKey(slug, "project"))?.detected ??
          null),
    [settings, detectedProp, slug, queryClient],
  );
  const [agents, setAgents] = useState<string[]>(() =>
    settings
      ? [...configured]
      : [...new Set((detected?.detected_agents ?? []).map((d) => d.agent))],
  );
  const [removedState, setRemoved] = useState<string[]>([]);
  const [force, setForce] = useState(false);
  // In settings the removals are exactly the configured agents that were unticked.
  const removed = settings ? configured.filter((a) => !agents.includes(a)) : removedState;
  const [confirmed, setConfirmed] = useState<ReadonlySet<string>>(new Set());
  const [done, setDone] = useState<InitResult | null>(null);
  // A linked project's Finish with no agent asks once more (ONB-7).
  const [noAgentOk, setNoAgentOk] = useState(false);

  const agentActions = useMemo(
    () => Object.fromEntries(removed.map((a) => [a, "remove" as const])),
    [removed],
  );

  const preview = useQuery({
    queryKey: projectKey(slug, "init-preview", [...agents].sort(), [...removed].sort(), force),
    queryFn: () =>
      projectApi(slug).init({ agents, agent_actions: agentActions, dry_run: true, ...(force && { force }) }),
    placeholderData: keepPreviousData,
    retry: false,
    staleTime: 0,
  });

  const pending = (preview.data?.agent_files ?? [])
    .filter((f) => f.status === "needs_confirmation")
    .map((f) => f.file);
  const unconfirmed = pending.filter((f) => !confirmed.has(f));

  const init = useMutation({
    mutationFn: () =>
      projectApi(slug).init({
        agents,
        agent_actions: agentActions,
        confirm_tracked: pending.filter((f) => confirmed.has(f)),
        ...(force && { force }),
      }),
    onSuccess: (result) => {
      setDone(result);
      // Mark the cached project initialized at once: the wizard routes on this
      // flag, and the next step may render before the refetch lands.
      if (result.initialized) {
        queryClient.setQueryData<ProjectDetails>(projectKey(slug, "project"), (old) =>
          old ? { ...old, initialized: true } : old,
        );
      }
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
      void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "init-preview") });
      if (!result.initialized) return;
      if (settings) {
        toast.success("Agent changes applied");
        return;
      }
      // Straight on to Configure unless there is something to read first.
      if (!linked && !needsReading(result)) onDone(result);
    },
  });

  const panelDetected: Detected | null = useMemo(() => {
    const custom = preview.data?.custom_db_paths ?? detected?.custom_db_paths ?? [];
    if (!detected && custom.length === 0) return null;
    return { ...(detected ?? EMPTY), custom_db_paths: custom };
  }, [detected, preview.data]);

  const setAction = (agent: string, action: "migrate" | "remove") => {
    if (action === "remove") {
      setAgents((a) => a.filter((x) => x !== agent));
      setRemoved((r) => (r.includes(agent) ? r : [...r, agent]));
    } else {
      setRemoved((r) => r.filter((x) => x !== agent));
      setAgents((a) => (a.includes(agent) ? a : [...a, agent]));
    }
  };

  if (done?.initialized && settings) {
    return (
      <InitDone
        result={done}
        title="Agent files updated"
        flat
        actions={
          <Button
            onClick={() => {
              setDone(null);
              onDone();
            }}
          >
            Done
          </Button>
        }
      />
    );
  }
  if (done?.initialized && linked) return <LinkedDone slug={slug} result={done} project={project.data} />;
  if (done?.initialized) {
    return <WizardDone slug={slug} result={done} onContinue={onDone} />;
  }

  const noAgent = !settings && linked && agents.length === 0;
  const blocked = !preview.data || preview.isError || unconfirmed.length > 0;
  const submit = () => {
    if (noAgent && !noAgentOk) {
      setNoAgentOk(true);
      return;
    }
    init.mutate();
  };
  const label = init.isPending
    ? settings
      ? "Applying…"
      : linked
        ? "Finishing…"
        : "Setting up…"
    : settings
      ? "Apply changes"
      : linked
        ? noAgent && noAgentOk
          ? "Finish without an agent"
          : "Finish"
        : "Set up project";

  return (
    <div className="flex flex-col gap-5">
      {panelDetected && !settings && (
        <DetectedPanel detected={panelDetected} removed={removed} onActionChange={setAction} />
      )}

      <Block flat={settings}>
        {!settings && (
          <div>
            <h2 className="text-sm font-semibold">Agents</h2>
            <p className="text-xs text-muted-foreground">
              Pick the coding agents that should use WhyGraph in this repository. Each gets its own
              settings file and a few instruction files.
            </p>
          </div>
        )}
        <AgentPicker
          value={agents}
          onChange={(next) => {
            setAgents(next);
            setNoAgentOk(false);
          }}
          detected={detected?.detected_agents}
          removed={settings ? [] : removed}
          port={port}
        />
        {noAgent && (
          <Alert variant="warning" data-testid="no-agent-warning">
            <TriangleAlertIcon />
            <AlertDescription>
              Pick at least one agent - without one, nothing on this machine uses the link.
            </AlertDescription>
          </Alert>
        )}
        {settings && removed.length > 0 && (
          <p className="text-xs text-muted-foreground" data-testid="removal-note">
            The whygraph entry will be removed from the config file of: {removed.join(", ")}.
          </p>
        )}
        {settings && (
          <div className="flex flex-col gap-2 border-t border-border pt-3">
            <label className="flex cursor-pointer items-start gap-2 text-sm">
              <Checkbox checked={force} onCheckedChange={setForce} className="mt-0.5" />
              <span>
                Update agent files
                <span className="block text-xs text-muted-foreground">
                  Rewrite the instruction files WhyGraph installed for the selected agents with this
                  version's copies.
                </span>
              </span>
            </label>
            {force && (
              <Alert data-testid="force-warning">
                <TriangleAlertIcon />
                <AlertTitle>Local edits to these files are replaced</AlertTitle>
                <AlertDescription>
                  Check the preview below. A backup of each rewritten file goes to{" "}
                  <span className="font-mono">.whygraph/backups/</span>.
                </AlertDescription>
              </Alert>
            )}
          </div>
        )}
      </Block>

      <Block flat={settings}>
        <div>
          <h2 className={cn("font-semibold", settings ? "text-[13px]" : "text-sm")}>What will change</h2>
          <p className="text-xs text-muted-foreground">
            {!settings && "Nothing is written until you set the project up. "}A backup of any file that is
            rewritten goes to <span className="font-mono">.whygraph/backups/</span>.
          </p>
        </div>
        {preview.data?.ignored_db && (
          <Alert data-testid="ignored-db">
            <AlertTitle>An older database is ignored</AlertTitle>
            <AlertDescription>
              <span className="font-mono break-all">{preview.data.ignored_db}</span> is left over from an earlier
              local project in this folder. A linked project keeps its history on the platform, so this
              file is never opened or changed.
            </AlertDescription>
          </Alert>
        )}
        {preview.isError && (
          <ErrorState error={preview.error} title="Couldn't preview the changes" onRetry={() => void preview.refetch()} />
        )}
        {!preview.data && preview.isLoading && (
          <p className="text-sm text-muted-foreground">Checking the repository…</p>
        )}
        {preview.data && (
          <InitPreview
            result={preview.data}
            selectedAgents={agents}
            setupRows={!settings}
            confirmed={confirmed}
            onConfirmChange={(file, on) =>
              setConfirmed((prev) => {
                const next = new Set(prev);
                if (on) next.add(file);
                else next.delete(file);
                return next;
              })
            }
          />
        )}
      </Block>

      {init.isError && (
        <ErrorState error={init.error} title={settings ? "Couldn't apply the changes" : "Couldn't set the project up"} />
      )}
      {done && !done.initialized && (
        <Alert>
          <AlertTitle>Not finished yet</AlertTitle>
          <AlertDescription>
            These files are committed to git and still await your confirmation:{" "}
            <span className="font-mono break-all">{done.needs_confirmation.join(", ")}</span>.
          </AlertDescription>
        </Alert>
      )}

      <div className="row-wrap items-center justify-end gap-2">
        {unconfirmed.length > 0 && (
          <span className="mr-auto text-xs text-muted-foreground" data-testid="confirm-hint">
            Confirm the committed file{unconfirmed.length > 1 ? "s" : ""} above, or deselect{" "}
            {unconfirmed.length > 1 ? "their agents" : "its agent"}, to continue.
          </span>
        )}
        <Button onClick={submit} disabled={blocked || init.isPending}>
          {label}
        </Button>
      </div>
    </div>
  );
}

/** Whether a Set up result has something the person should read before moving on. */
function needsReading(result: InitResult): boolean {
  return (
    result.agent_files.some((f) => f.status === "refused") || !!result.hooks_error || !!result.scan_error
  );
}

/** The registry's sentence for the scan the runner refused to queue (`scan_error`). */
function scanErrorInfo(code: string) {
  return errorInfo(new ApiError(409, "", code));
}

/** **Start the first scan**, for a Set up whose scan the runner refused. */
function useStartFirstScan(slug: string, onStarted: (runId: number) => void) {
  return useMutation({
    mutationFn: () => projectApi(slug).requestScan({ trigger: "manual", analyze: false }),
    onSuccess: ({ run_id }) => onStarted(run_id),
  });
}

function ScanRefused({
  slug,
  code,
  onStarted,
}: {
  slug: string;
  code: string;
  onStarted: (runId: number) => void;
}) {
  const start = useStartFirstScan(slug, onStarted);
  const info = scanErrorInfo(code);
  return (
    <Alert variant="warning" data-testid="scan-error">
      <TriangleAlertIcon />
      <AlertTitle>The first scan didn't start</AlertTitle>
      <AlertDescription>
        <p>{info.message}</p>
        <div className="mt-2 flex flex-col items-start gap-2">
          <Button size="sm" onClick={() => start.mutate()} disabled={start.isPending}>
            {start.isPending ? "Starting…" : "Start the first scan"}
          </Button>
          {start.isError && <ErrorState error={start.error} size="inline" />}
        </div>
      </AlertDescription>
    </Alert>
  );
}

/** A local project's done panel, only when something needs reading before Configure. */
function WizardDone({
  slug,
  result,
  onContinue,
}: {
  slug: string;
  result: InitResult;
  onContinue: (result: InitResult) => void;
}) {
  return (
    <InitDone
      result={result}
      title="Project set up"
      extra={
        result.scan_error && !result.initial_run_id ? (
          <ScanRefused
            slug={slug}
            code={result.scan_error}
            onStarted={(runId) => onContinue({ ...result, initial_run_id: runId })}
          />
        ) : null
      }
      actions={<Button onClick={() => onContinue(result)}>Continue to Configure</Button>}
    />
  );
}

/**
 * A linked project's last step (Source -> Set up): what was set up, its first
 * scan (a CodeGraph index of the checkout), and "Connect your agent".
 */
function LinkedDone({
  slug,
  result,
  project,
}: {
  slug: string;
  result: InitResult;
  project: ProjectDetails | undefined;
}) {
  const navigate = useNavigate();
  const [runId, setRunId] = useState<number | null>(result.initial_run_id ?? null);
  return (
    <div className="flex flex-col gap-5">
      <InitDone result={result} title="Project linked" />
      {runId !== null ? (
        <RunCard slug={slug} runId={runId} />
      ) : (
        result.scan_error && <ScanRefused slug={slug} code={result.scan_error} onStarted={setRunId} />
      )}
      {project?.mcp_url && <ConnectAgent mcpUrl={project.mcp_url} configured={result.configured_agents} />}
      {result.configured_agents.length === 0 && (
        <Alert variant="info">
          <InfoIcon />
          <AlertDescription>
            No agent uses the link yet. Add one in the project's settings, under Agents.
          </AlertDescription>
        </Alert>
      )}
      <div className="flex justify-end">
        <Button onClick={() => void navigate({ to: "/p/$slug", params: { slug } })}>Open project</Button>
      </div>
    </div>
  );
}

function InitDone({
  result,
  title,
  actions,
  extra,
  flat = false,
}: {
  result: InitResult;
  title: string;
  actions?: React.ReactNode;
  extra?: React.ReactNode;
  flat?: boolean;
}) {
  const refused = result.agent_files.filter((f) => f.status === "refused");
  return (
    <div className="flex flex-col gap-4" data-testid="init-done">
      <Block flat={flat}>
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <CheckCircle2Icon className="size-4 text-success" />
          {title}
        </h2>
        <ul className="flex flex-col gap-1 text-sm text-muted-foreground">
          <li>
            Agents connected:{" "}
            <span className="text-foreground">
              {result.configured_agents.length ? result.configured_agents.map(agentLabel).join(", ") : "none"}
            </span>
          </li>
          {result.hooks && result.hooks.installed.length > 0 && (
            <li>
              Git hooks installed:{" "}
              <span className="font-mono text-foreground">{result.hooks.installed.join(", ")}</span>
            </li>
          )}
          {result.gitignore_added.length > 0 && (
            <li>
              Added to <span className="font-mono">.gitignore</span>:{" "}
              <span className="font-mono text-foreground">{result.gitignore_added.join(", ")}</span>
            </li>
          )}
        </ul>
        {result.hooks_error && (
          <Alert>
            <AlertTitle>Git hooks were not installed</AlertTitle>
            <AlertDescription>{result.hooks_error}</AlertDescription>
          </Alert>
        )}
      </Block>
      {refused.map((f) => (
        <Alert key={f.file}>
          <TriangleAlertIcon />
          <AlertTitle>
            Add the entry to <span className="font-mono break-all">{f.file}</span> yourself
          </AlertTitle>
          <AlertDescription>
            <p>{f.reason ?? "The file could not be merged safely, so it was left alone."}</p>
            {f.snippet && (
              <div className="mt-2 flex flex-col gap-2">
                <pre
                  data-scroll-x
                  className="max-h-48 overflow-auto rounded-md bg-muted p-2 font-mono text-xs text-foreground"
                >
                  {f.snippet}
                </pre>
                <div>
                  <CopyButton text={f.snippet} label="Copy snippet" />
                </div>
              </div>
            )}
          </AlertDescription>
        </Alert>
      ))}
      {extra}
      {actions && <div className="flex justify-end">{actions}</div>}
    </div>
  );
}
