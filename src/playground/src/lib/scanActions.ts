import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { portalKey, projectApi, projectKey } from "../api";

/** What a scan request carries; every call site says what it wants (no default). */
export type ScanBody = { trigger: "manual" | "describe"; analyze?: boolean };

/**
 * "Scan now" for one project (on a production project it fetches first). The portal coalesces requests: while a
 * job runs, every request joins the one pending run and gets the same id back.
 * So a call from a page showing an *active* run (`current.active`) stays put and
 * reports the follow-up (`followUp`), or says the run was already queued when the
 * id comes back equal to the current one; from anywhere else it opens the run.
 */
export function useScanActions(slug: string, current?: { id: number; active: boolean }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [followUp, setFollowUp] = useState<number | null>(null);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: projectKey(slug, "scans") });
    void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
  };

  const landed = (runId: number, what: string) => {
    refresh();
    if (current?.active) {
      if (runId === current.id) {
        toast.info(`${what} is already queued - this run will cover it`);
      } else if (runId === followUp) {
        toast.info(`${what} is already queued as run #${runId}`);
      } else {
        setFollowUp(runId);
        toast.success(`${what} queued as run #${runId}`);
      }
      return;
    }
    void navigate({
      to: "/p/$slug/scans/{-$runId}",
      params: { slug, runId: String(runId) },
    });
  };

  const scan = useMutation({
    mutationFn: (body: ScanBody) => projectApi(slug).requestScan(body),
    onSuccess: ({ run_id }) => landed(run_id, "Scan"),
    onError: (err) => toast.error(err.message),
  });

  return {
    scanNow: (body: ScanBody) => scan.mutate(body),
    scanPending: scan.isPending,
    /** The run a click during an active run was folded into, if it is not the current one. */
    followUp,
  };
}
