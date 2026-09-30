import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2Icon, TriangleAlertIcon } from "lucide-react";
import { portalKey, projectApi, projectKey, type Detected, type InitResult } from "../../api";
import { loadDetected } from "../../lib/detected";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { AgentPicker } from "./AgentPicker";
import { CopyButton } from "./CopyButton";
import { DetectedPanel } from "./DetectedPanel";
import { InitPreview } from "./InitPreview";

const EMPTY: Detected = {
  existing_db: false,
  managed_hooks: [],
  detected_agents: [],
  custom_db_paths: [],
};

/**
 * Step 3 of the wizard (screen 6): pick agents, preview exactly which files will
 * change, then initialize. A git-tracked agent file needs its confirm box ticked
 * before *Initialize* enables (deselect its agent to leave it alone). After a
 * successful run the step shows what was done, including the snippet for any file
 * WhyGraph refused to touch, before moving on to the first scan.
 */
export function InitializeStep({ slug, onDone }: { slug: string; onDone: () => void }) {
  const queryClient = useQueryClient();
  const detected = useMemo(() => loadDetected(slug), [slug]);
  const [agents, setAgents] = useState<string[]>(() => [
    ...new Set((detected?.detected_agents ?? []).map((d) => d.agent)),
  ]);
  const [removed, setRemoved] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState<ReadonlySet<string>>(new Set());
  const [done, setDone] = useState<InitResult | null>(null);

  const agentActions = useMemo(
    () => Object.fromEntries(removed.map((a) => [a, "remove" as const])),
    [removed],
  );

  const preview = useQuery({
    queryKey: projectKey(slug, "init-preview", [...agents].sort(), [...removed].sort()),
    queryFn: () => projectApi(slug).init({ agents, agent_actions: agentActions, dry_run: true }),
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
      }),
    onSuccess: (result) => {
      setDone(result);
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
      void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "init-preview") });
      if (result.initialized) toast.success("Project initialized");
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

  if (done?.initialized) return <InitDone result={done} onContinue={onDone} />;

  const blocked = !preview.data || preview.isError || unconfirmed.length > 0;
  return (
    <div className="flex flex-col gap-5">
      {panelDetected && (
        <DetectedPanel detected={panelDetected} removed={removed} onActionChange={setAction} />
      )}

      <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5">
        <div>
          <h2 className="text-sm font-semibold">Agents</h2>
          <p className="text-xs text-muted-foreground">
            Pick the agents that should connect to this project's WhyGraph MCP server. The repository
            gets a config entry and a few instruction files for each.
          </p>
        </div>
        <AgentPicker
          value={agents}
          onChange={setAgents}
          detected={detected?.detected_agents}
          removed={removed}
        />
      </section>

      <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5">
        <div>
          <h2 className="text-sm font-semibold">What will change</h2>
          <p className="text-xs text-muted-foreground">
            Nothing is written until you initialize. A backup of any file that is rewritten goes to
            <span className="font-mono"> .whygraph/backups/</span>.
          </p>
        </div>
        {preview.isError && (
          <Alert variant="destructive">
            <AlertTitle>Could not preview the changes</AlertTitle>
            <AlertDescription>{preview.error.message}</AlertDescription>
          </Alert>
        )}
        {!preview.data && preview.isLoading && (
          <p className="text-sm text-muted-foreground">Checking the repository…</p>
        )}
        {preview.data && (
          <InitPreview
            result={preview.data}
            selectedAgents={agents}
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
      </section>

      {init.isError && (
        <Alert variant="destructive">
          <AlertTitle>Initialization failed</AlertTitle>
          <AlertDescription>{init.error.message}</AlertDescription>
        </Alert>
      )}
      {done && !done.initialized && (
        <Alert>
          <AlertTitle>Not finished yet</AlertTitle>
          <AlertDescription>
            These files are committed to git and still await your confirmation:{" "}
            <span className="font-mono">{done.needs_confirmation.join(", ")}</span>.
          </AlertDescription>
        </Alert>
      )}

      <div className="flex items-center justify-end gap-2">
        {unconfirmed.length > 0 && (
          <span className="mr-auto text-xs text-muted-foreground" data-testid="confirm-hint">
            Confirm the committed file{unconfirmed.length > 1 ? "s" : ""} above, or deselect{" "}
            {unconfirmed.length > 1 ? "their agents" : "its agent"}, to continue.
          </span>
        )}
        <Button variant="ghost" render={<Link to="/p/$slug/init" params={{ slug }} search={{ step: "configure" }} />}>
          Back
        </Button>
        <Button onClick={() => init.mutate()} disabled={blocked || init.isPending}>
          {init.isPending ? "Initializing…" : "Initialize"}
        </Button>
      </div>
    </div>
  );
}

function InitDone({ result, onContinue }: { result: InitResult; onContinue: () => void }) {
  const refused = result.agent_files.filter((f) => f.status === "refused");
  return (
    <div className="flex flex-col gap-4" data-testid="init-done">
      <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <CheckCircle2Icon className="size-4 text-success" />
          Project initialized
        </h2>
        <ul className="flex flex-col gap-1 text-sm text-muted-foreground">
          <li>
            Agents connected:{" "}
            <span className="text-foreground">
              {result.configured_agents.length ? result.configured_agents.join(", ") : "none"}
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
      </section>
      {refused.map((f) => (
        <Alert key={f.file}>
          <TriangleAlertIcon />
          <AlertTitle>
            Add the entry to <span className="font-mono">{f.file}</span> yourself
          </AlertTitle>
          <AlertDescription>
            <p>{f.reason ?? "The file could not be merged safely, so it was left alone."}</p>
            {f.snippet && (
              <div className="mt-2 flex flex-col gap-2">
                <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 font-mono text-xs text-foreground">
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
      <div className="flex justify-end">
        <Button onClick={onContinue}>Continue to first scan</Button>
      </div>
    </div>
  );
}
