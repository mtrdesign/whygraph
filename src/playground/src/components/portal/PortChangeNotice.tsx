import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NetworkIcon } from "lucide-react";
import {
  portalApi,
  portalKey,
  type PortChange,
  type PortChangeAgent,
  type PortChangeProject,
  type ProjectPortChange,
} from "../../api";
import { dismissPort, dismissedPort, summarizePortChange } from "../../lib/portChange";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { CopyButton } from "./CopyButton";

// Acceptance #27: when the portal starts on a new port it rewrites what it can
// (markers, untracked literal agent entries) and reports the rest. These notices
// show that report - what changed, the exact line to edit by hand in a tracked
// file, the env hint for env-interpolated entries, and the folders it could not
// reach - until dismissed for that port.

function useDismissal(scope: string, port: number) {
  const [dismissed, setDismissed] = useState(() => dismissedPort(scope) === port);
  return [
    dismissed,
    () => {
      dismissPort(scope, port);
      setDismissed(true);
    },
  ] as const;
}

function AgentLine({ agent }: { agent: PortChangeAgent }) {
  const file = <span className="font-mono">{agent.file}</span>;
  switch (agent.action) {
    case "rewritten":
      return <li>{file}: updated to the new port.</li>;
    case "up_to_date":
      return <li>{file}: already up to date.</li>;
    case "env":
      return (
        <li>
          {file}: reads the port from your environment, so it was not changed - {agent.hint}.
        </li>
      );
    case "skipped":
      return (
        <li>
          {file}: not checked{agent.reason ? ` (${agent.reason})` : ""}.
        </li>
      );
    case "manual":
      return (
        <li data-testid={`port-manual-${agent.agent}`}>
          <p>
            {file}: not changed{agent.reason ? ` (${agent.reason})` : ""}. Set this line yourself:
          </p>
          {agent.line && (
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <code className="min-w-0 flex-1 overflow-x-auto rounded-md bg-muted px-2 py-1 font-mono text-xs text-foreground">
                {agent.line}
              </code>
              <CopyButton text={agent.line} />
            </div>
          )}
          {(agent.diff || agent.snippet) && (
            <details className="mt-1">
              <summary className="cursor-pointer text-xs">{agent.diff ? "Show the change" : "Show the entry"}</summary>
              <pre className="mt-1 overflow-auto rounded-md bg-muted p-2 text-xs text-foreground">
                {agent.diff || agent.snippet}
              </pre>
            </details>
          )}
        </li>
      );
  }
}

function ProjectChanges({ item, showName }: { item: PortChangeProject; showName: boolean }) {
  return (
    <div className="flex flex-col gap-1" data-testid={`port-change-${item.slug}`}>
      {showName && (
        <p className="text-foreground">
          <span className="font-medium">{item.slug}</span>{" "}
          <span className="font-mono text-xs text-muted-foreground">{item.root}</span>
        </p>
      )}
      <ul className="list-disc pl-4">
        <li>
          {item.markers === "rewritten"
            ? `Portal markers updated (were port ${item.previous_port}).`
            : `Portal markers not updated${item.reason ? `: ${item.reason}` : ""}.`}
        </li>
        {item.agents.map((a) => (
          <AgentLine key={`${a.agent}:${a.file}`} agent={a} />
        ))}
      </ul>
    </div>
  );
}

/**
 * Portal-level banner (Projects page): the whole port-change report from
 * `GET /api/portal/state`. Hidden when there is none or it was dismissed for this port.
 */
export function PortChangeBanner() {
  const state = useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state });
  const change = state.data?.port_change ?? null;
  if (!change) return null;
  return <PortChangeBannerBody key={change.port} change={change} />;
}

function PortChangeBannerBody({ change }: { change: PortChange }) {
  const [dismissed, dismiss] = useDismissal("portal", change.port);
  if (dismissed) return null;
  const counts = summarizePortChange(change);
  return (
    <Alert data-testid="port-change-banner">
      <NetworkIcon />
      <AlertTitle>
        The portal now runs on port {change.port}
        {change.previous_port !== null ? ` (was ${change.previous_port})` : ""}
      </AlertTitle>
      <AlertDescription>
        <p>
          Agents and git hooks reach the portal by its port. WhyGraph updated {counts.rewritten} file
          {counts.rewritten === 1 ? "" : "s"}
          {counts.manual > 0 ? `; ${counts.manual} need${counts.manual === 1 ? "s" : ""} a manual edit` : ""}.
        </p>
        <div className="mt-2 flex flex-col gap-3">
          {change.projects.map((item) => (
            <ProjectChanges key={item.slug} item={item} showName />
          ))}
          {change.unmounted.length > 0 && (
            <div data-testid="port-change-unmounted">
              <p>
                These folders were not available, so their markers and agent files still name the old
                port. They are updated the next time the portal starts with the folder available:
              </p>
              <ul className="list-disc pl-4">
                {change.unmounted.map((u) => (
                  <li key={u.slug}>
                    <span className="font-medium">{u.slug}</span>{" "}
                    <span className="font-mono text-xs">{u.root}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
        <div className="mt-3">
          <Button size="sm" variant="outline" onClick={dismiss}>
            Dismiss
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}

/** Per-project notice (overview, settings): that project's slice of the report. */
export function ProjectPortChangeNotice({
  slug,
  change,
}: {
  slug: string;
  change: ProjectPortChange | null | undefined;
}) {
  const state = useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state });
  if (!change) return null;
  // A mounted item names only the old port; the new one is the portal's own.
  const port = "unmounted" in change ? change.port : (state.data?.port ?? change.previous_port);
  return <ProjectPortChangeBody key={`${slug}:${port}`} slug={slug} port={port} change={change} />;
}

function ProjectPortChangeBody({
  slug,
  port,
  change,
}: {
  slug: string;
  port: number;
  change: ProjectPortChange;
}) {
  const unmounted = "unmounted" in change;
  const [dismissed, dismiss] = useDismissal(`project:${slug}`, port);
  if (dismissed) return null;
  return (
    <Alert data-testid="project-port-change">
      <NetworkIcon />
      <AlertTitle>The portal moved to port {port}</AlertTitle>
      <AlertDescription>
        {unmounted ? (
          <p>
            The portal now runs on port {port}, but this folder was not available when it
            started, so its markers and agent files still name the old port. They are updated the
            next time the portal starts with the folder available.
          </p>
        ) : (
          <ProjectChanges item={change} showName={false} />
        )}
        <div className="mt-2">
          <Button size="sm" variant="outline" onClick={dismiss}>
            Dismiss
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}
