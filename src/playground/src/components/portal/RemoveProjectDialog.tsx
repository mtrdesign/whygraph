import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ApiError,
  portalKey,
  projectApi,
  type DeleteProjectResult,
  type FileOutcome,
  type ProjectDetails,
} from "../../api";
import { agentInfo } from "../../lib/agents";
import { isProduction, usePortalState } from "../../lib/identity";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Checkbox } from "../ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import { Input } from "../ui/input";
import { DiffView } from "./InitPreview";

/**
 * "Remove project": says exactly what is and is not deleted, offers to strip the
 * agents' MCP entries too, and for a GitHub clone asks for the project's name
 * (the checkout is deleted). A git-tracked agent file the removal would change
 * comes back from the backend (`needs_confirmation`) with its diff and is
 * confirmed in place. Removal is refused while a scan runs. In production a
 * project is a server copy of a GitHub repository with no hooks or agent
 * files, so the dialog says only that the copy and its scans go.
 */
export function RemoveProjectDialog({
  project,
  open,
  onOpenChange,
}: {
  project: ProjectDetails;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const github = project.source === "github";
  const production = isProduction(usePortalState().data);
  const [strip, setStrip] = useState(false);
  const [typed, setTyped] = useState("");
  const [pending, setPending] = useState<FileOutcome[]>([]);
  const [confirmed, setConfirmed] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DeleteProjectResult | null>(null);
  const agentFiles = project.agents.map((a) => agentInfo(a)?.file ?? a);

  const remove = useMutation({
    mutationFn: () =>
      projectApi(project.slug).remove({
        strip_agent_entries: strip,
        confirm_tracked: [...confirmed],
        ...(github && { confirm_name: typed }),
      }),
    onSuccess: (r) => {
      setError(null);
      setResult(r);
      void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
      if (r.warnings.length > 0) toast.warning(`Removed with ${r.warnings.length} warning(s)`);
      else toast.success(`${project.name} removed`);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "needs_confirmation") {
        setPending((err.extra.agent_files as FileOutcome[] | undefined) ?? []);
        setError(null);
        return;
      }
      setError(err.message);
    },
  });

  const unconfirmed = pending.filter((f) => !confirmed.has(f.file));
  const busy = !!project.running_scan;
  const nameOk = !github || typed === project.name;
  const canRemove = nameOk && !busy && unconfirmed.length === 0 && !remove.isPending;

  // Leave first: dropping the cache while this page is still mounted would make
  // its queries refetch a project that no longer exists (404s).
  const leave = () => {
    void navigate({ to: "/" }).then(() => queryClient.removeQueries({ queryKey: [project.slug] }));
  };

  if (result) {
    return (
      <Dialog open={open} onOpenChange={(o) => (o ? onOpenChange(o) : leave())}>
        <DialogContent data-testid="remove-done">
          <DialogHeader>
            <DialogTitle>{project.name} was removed</DialogTitle>
            <DialogDescription>
              {result.checkout_deleted
                ? production
                  ? "The server copy was deleted. The repository on GitHub was not changed."
                  : "The checkout was deleted."
                : "Your repository was left as it was."}
            </DialogDescription>
          </DialogHeader>
          {result.warnings.length > 0 && (
            <Alert>
              <AlertTitle>Some things were left alone</AlertTitle>
              <AlertDescription>
                <ul className="list-disc pl-4">
                  {result.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            <Button onClick={leave}>Back to projects</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg" data-testid="remove-dialog">
        <DialogHeader>
          <DialogTitle>Remove {project.name}?</DialogTitle>
          <DialogDescription>
            {production
              ? "This removes the server copy and every scan."
              : "This unregisters the project from the portal."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3 text-sm">
          {production ? (
            <>
              <div>
                <p className="font-medium">Removed</p>
                <ul className="list-disc pl-5 text-muted-foreground">
                  <li>The project's settings, keys and scan history.</li>
                  <li>
                    The server copy of the repository at <span className="font-mono">{project.root}</span>.
                  </li>
                </ul>
              </div>
              <div>
                <p className="font-medium">Kept</p>
                <ul className="list-disc pl-5 text-muted-foreground">
                  <li>The repository on GitHub; nothing is changed there.</li>
                </ul>
              </div>
            </>
          ) : (
            <>
              <div>
                <p className="font-medium">Removed</p>
                <ul className="list-disc pl-5 text-muted-foreground">
                  <li>The portal's record of the project: its settings, keys and scan history.</li>
                  <li>
                    The managed git hooks and the <span className="font-mono">.whygraph/portal.*</span>{" "}
                    markers in the repository.
                  </li>
                  {github && (
                    <li>
                      The cloned checkout at <span className="font-mono">{project.root}</span>.
                    </li>
                  )}
                </ul>
              </div>
              <div>
                <p className="font-medium">Kept</p>
                <ul className="list-disc pl-5 text-muted-foreground">
                  {!github && (
                    <li>
                      Your repository, including its <span className="font-mono">.whygraph/</span> and{" "}
                      <span className="font-mono">.codegraph/</span> data. <span className="font-mono">whygraph scan</span>{" "}
                      works in it again.
                    </li>
                  )}
                  {github && <li>The repository on GitHub; nothing is changed there.</li>}
                  <li>The agents' MCP entries, unless you tick the box below.</li>
                </ul>
              </div>

              <label className="flex cursor-pointer items-start gap-2">
                <Checkbox checked={strip} onCheckedChange={setStrip} className="mt-0.5" />
                <span>
                  Also remove the agent MCP entries
                  <span className="block text-xs text-muted-foreground">
                    {agentFiles.length > 0 ? (
                      <>
                        Removes the whygraph entry from{" "}
                        <span className="font-mono">{agentFiles.join(", ")}</span>.
                      </>
                    ) : (
                      "No agent was set up through the portal, so there is nothing to remove."
                    )}
                  </span>
                </span>
              </label>
            </>
          )}

          {github && (
            <div className="flex flex-col gap-1.5">
              <label htmlFor="confirm-name" className="text-sm">
                Type <span className="font-mono font-medium">{project.name}</span> to confirm deleting the{" "}
                {production ? "server copy" : "checkout"}
              </label>
              <Input id="confirm-name" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
            </div>
          )}

          {pending.length > 0 && (
            <div className="flex flex-col gap-3" data-testid="remove-confirm-tracked">
              <p className="font-medium">Committed files will change</p>
              {pending.map((f) => (
                <div key={f.file} className="flex flex-col gap-2">
                  <span className="font-mono text-xs">{f.file}</span>
                  {f.diff && <DiffView diff={f.diff} />}
                  <label className="flex cursor-pointer items-start gap-2 text-sm">
                    <Checkbox
                      checked={confirmed.has(f.file)}
                      onCheckedChange={(on) =>
                        setConfirmed((prev) => {
                          const next = new Set(prev);
                          if (on) next.add(f.file);
                          else next.delete(f.file);
                          return next;
                        })
                      }
                      className="mt-0.5"
                    />
                    <span>This file is committed to git. Change it as shown.</span>
                  </label>
                </div>
              ))}
            </div>
          )}

          {busy && (
            <Alert>
              <AlertDescription>A scan is queued or running. Wait for it to finish first.</AlertDescription>
            </Alert>
          )}
          {error && (
            <Alert variant="destructive" data-testid="remove-error">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={!canRemove} onClick={() => remove.mutate()}>
            {remove.isPending ? "Removing…" : "Remove project"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
