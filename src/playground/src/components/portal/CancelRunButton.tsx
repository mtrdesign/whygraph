import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
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
 * Commits a scan already described are kept, so nothing is redone next time.
 */
export function CancelRunButton({
  slug,
  runId,
  status,
  kind,
}: {
  slug: string;
  runId: number;
  status: "queued" | "running";
  kind: "scan" | "sync";
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancel = useMutation({
    mutationFn: () => projectApi(slug).cancelScan(runId),
    onSuccess: (r) => {
      setOpen(false);
      toast.success(r.was === "queued" ? `Run #${runId} removed from the queue` : `Stopping run #${runId}`);
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "scans") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    },
    onError: (err) => setError(err.message),
  });
  const noun = kind === "sync" ? "sync" : "scan";

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
            <DialogTitle>
              Cancel {noun} #{runId}?
            </DialogTitle>
            <DialogDescription>
              {status === "queued"
                ? `It has not started yet, so it is simply removed from the queue.`
                : `The ${noun} stops now. Commits it already described are kept, and the next scan picks up from there.`}
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
              Cancel {noun}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
