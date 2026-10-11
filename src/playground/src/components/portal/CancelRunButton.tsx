import { errorMessage } from "../../lib/apiErrors";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { projectApi, projectKey } from "../../api";
import { Alert, AlertDescription } from "../ui/alert";
import { Button } from "../ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";

/**
 * "Cancel" on a queued or running run, behind a confirm. A queued run leaves the
 * queue at once; a running one is stopped (its child gets SIGTERM, SIGKILL after
 * 10 s) and its event stream ends with `cancelled`, which the run view shows.
 * The dialog says what is kept by kind: described commits for a full run, the last
 * finished scan's data for a structure-only one.
 */
export function CancelRunButton({
  slug,
  runId,
  status,
  kind,
  analyze,
}: {
  slug: string;
  runId: number;
  status: "queued" | "running";
  kind: "scan" | "sync";
  /** Whether the run describes commits (a full run): decides what the dialog promises is kept. */
  analyze: boolean;
}) {
  const queryClient = useQueryClient();
  const noun = kind === "sync" ? "sync" : "scan";
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancel = useMutation({
    mutationFn: () => projectApi(slug).cancelScan(runId),
    onSuccess: () => {
      setOpen(false);
      // No toast: the run page itself turns to "Cancelled by you" (CN-1).
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "scans") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    },
    onError: (err) => setError(errorMessage(err)),
  });

  return (
    <>
      <Button
        variant="outline"
        data-testid="cancel-run"
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
      >
        Cancel
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent data-testid="cancel-run-dialog">
          <DialogHeader>
            <DialogTitle>{status === "queued" ? `Remove this ${noun} from the queue?` : `Stop this ${noun}?`}</DialogTitle>
            <DialogDescription>
              {status === "queued"
                ? "It has not started yet, so it is simply removed from the queue."
                : analyze
                  ? "Commits described so far are kept."
                  : "The project keeps the data from its last finished scan."}
            </DialogDescription>
          </DialogHeader>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              Keep it
            </Button>
            <Button variant="destructive" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
              {status === "queued" ? "Remove from queue" : `Stop ${noun}`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
