import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { portalKey, projectApi, projectKey } from "../api";
import { errorInfo } from "./apiErrors";

/** What a scan request carries; every call site says what it wants (no default). */
export type ScanBody = { trigger: "manual" | "describe"; analyze?: boolean };

/**
 * "Scan now" for one project (on a production project it fetches first). The portal coalesces requests: while a
 * job runs, every request joins the one pending run and gets the same id back.
 * So a call from a page showing an *active* run (`current.active`) stays put and
 * records the follow-up (`followUp`; the run page shows it, no toast), and stays put
 * when the id comes back equal to the current one; from anywhere else it opens the run.
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

  const open = (runId: number) =>
    void navigate({
      to: "/p/$slug/scans/{-$runId}",
      params: { slug, runId: String(runId) },
    });

  // The run id is only ever in a URL (SCN-4): a toast names the run by an
  // "Open run" action, never by "#7".
  const landed = (runId: number) => {
    refresh();
    if (current?.active) {
      // The run's own page already says it (the "Another request joined this
      // run" notice), so no toast over its buttons.
      if (runId !== current.id) setFollowUp(runId);
      return;
    }
    open(runId);
  };

  const scan = useMutation({
    mutationFn: (body: ScanBody) => projectApi(slug).requestScan(body),
    onSuccess: ({ run_id }) => landed(run_id),
    // A refusal (`budget_exceeded`, `forbidden`, ...) reads as the registry words it.
    onError: (err) => {
      const info = errorInfo(err);
      toast.error(info.title, { description: info.message });
    },
  });

  return {
    scanNow: (body: ScanBody) => scan.mutate(body),
    scanPending: scan.isPending,
    /** The run a click during an active run was folded into, if it is not the current one. */
    followUp,
  };
}
