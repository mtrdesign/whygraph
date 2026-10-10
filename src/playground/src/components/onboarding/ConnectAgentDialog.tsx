import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { portalApi, portalKey } from "../../api";
import { isProduction, usePortalState } from "../../lib/identity";
import { CopyButton } from "../portal/CopyButton";
import { nativeSelect } from "../portal/Field";
import { UseWithAgent } from "../portal/UseWithAgent";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../ui/dialog";
import { Label } from "../ui/label";

const DOCS = "https://mtrdesign.github.io/whygraph/portal/agents/";

/**
 * "Connect your agent" outside a project (ONB-6): install and start WhyGraph on
 * your machine, pick one of the projects you can read, then the per-project link
 * (`UseWithAgent`). With no project it says where to find the card instead.
 */
export function ConnectAgentDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const state = usePortalState().data;
  const production = isProduction(state);
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects, enabled: open });
  const [choice, setChoice] = useState("");
  const readable = (projects.data?.projects ?? []).filter(
    (p) => !p.importing && (!p.permissions || p.permissions.includes("project.read")),
  );
  const slug = choice || (readable.length === 1 ? readable[0].slug : "");
  const baseUrl = state?.base_url;
  const org = state?.org;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg" data-testid="connect-agent-dialog">
        <DialogHeader>
          <DialogTitle>Connect your agent</DialogTitle>
          <DialogDescription>
            Your coding agent reads this organization's history through WhyGraph running on your machine.
          </DialogDescription>
        </DialogHeader>
        <ol className="flex list-decimal flex-col gap-1.5 pl-5 text-sm">
          <li>
            Install WhyGraph - see the{" "}
            <a
              href="https://mtrdesign.github.io/whygraph/getting-started/installation/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary-text hover:underline"
            >
              installation guide
            </a>
            .
          </li>
          <li className="flex flex-wrap items-center gap-2">
            Start it: <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">whygraph up</code>
            <CopyButton text="whygraph up" />
          </li>
          <li>Pick a project below and open the link.</li>
        </ol>
        {projects.isSuccess && readable.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="connect-agent-no-projects">
            Open a project, then Use with your agent.
          </p>
        )}
        {readable.length > 1 && (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="connect-agent-project">Project</Label>
            <select
              id="connect-agent-project"
              className={nativeSelect()}
              value={slug}
              onChange={(e) => setChoice(e.target.value)}
            >
              <option value="">Choose a project…</option>
              {readable.map((p) => (
                <option key={p.slug} value={p.slug}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
        )}
        {slug && production && baseUrl && org && <UseWithAgent baseUrl={baseUrl} org={org.slug} slug={slug} />}
        {slug && !production && (
          <p className="text-sm text-muted-foreground">
            Open the project, then use the agent card on its Overview.
          </p>
        )}
        <p className="text-xs text-muted-foreground">
          <a href={DOCS} target="_blank" rel="noopener noreferrer" className="text-primary-text hover:underline">
            Read more about connecting an agent
          </a>
        </p>
      </DialogContent>
    </Dialog>
  );
}
