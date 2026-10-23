import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectApi, projectKey } from "../../api";
import { errorMessage } from "../../lib/apiErrors";
import { formatNumber } from "../../lib/format";
import { ErrorState } from "../state/ErrorState";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../ui/dialog";
import { ConfirmDialog } from "./ConfirmDialog";

/** "Open CodeGraph log": the tail of the one log every project's CodeGraph shares. */
export function CodegraphLogButton({ slug, label }: { slug: string; label: string }) {
  const [open, setOpen] = useState(false);
  const log = useQuery({
    queryKey: projectKey(slug, "codegraph-log"),
    queryFn: () => projectApi(slug).codegraphLog(),
    enabled: open,
    retry: false,
  });
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        {label}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent data-testid="codegraph-log-dialog" className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>CodeGraph log</DialogTitle>
            <DialogDescription>
              One log for the CodeGraph server every project on this portal shares.
            </DialogDescription>
          </DialogHeader>
          {log.isLoading && <p className="text-xs text-muted-foreground">Loading the log…</p>}
          {log.isError && <ErrorState error={log.error} context="codegraph" size="inline" onRetry={() => void log.refetch()} />}
          {log.data && (
            <>
              {log.data.truncated && (
                <p className="text-xs text-muted-foreground">
                  Showing the end of the log ({formatNumber(Math.round(log.data.size / 1024))} KiB in all). Keys
                  are redacted.
                </p>
              )}
              <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all">
                {log.data.text || "No output yet."}
              </pre>
            </>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

/** "Restart CodeGraph": the confirmation names the shared server, since it affects every project. */
export function CodegraphRestartButton({ slug, label }: { slug: string; label: string }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restart = useMutation({
    mutationFn: () => projectApi(slug).codegraphRestart(),
    onSuccess: () => {
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project-overview") });
    },
    onError: (err) => setError(errorMessage(err, "codegraph")),
  });
  return (
    <>
      <Button
        size="sm"
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
      >
        {label}
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Restart CodeGraph for every project?"
        description="This restarts the CodeGraph server for every project on this portal; other projects' calls retry automatically."
        confirmLabel="Restart CodeGraph"
        pending={restart.isPending}
        error={error}
        onConfirm={() => restart.mutate()}
      />
    </>
  );
}
