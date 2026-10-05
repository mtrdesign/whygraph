import { useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { portalApi, portalKey, projectApi, projectKey } from "../../api";
import { useScanRun } from "../../lib/scanRun";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { ScanEstimateCard } from "./ScanEstimateCard";
import { ScanProgress } from "./ScanProgress";

type Kind = "initial" | "describe";

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
      <h2 className="text-sm font-semibold">{title}</h2>
      {children}
    </section>
  );
}

/**
 * Step 4 of the wizard: the first scan runs structure-only (git history and code
 * structure, no LLM calls), then the cost card offers *Describe now* (a full scan
 * that writes descriptions) or *Later*. Resumable: an already-running scan is
 * re-attached to, and a project that has finished a scan goes straight to the
 * cost card. Progress is a simple view; step 13's scan-run screen replaces it.
 */
export function FirstScanStep({ slug }: { slug: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });
  const [runId, setRunId] = useState<number | null>(null);
  const [kind, setKind] = useState<Kind>("initial");
  const attached = runId === null ? (project.data?.running_scan?.id ?? null) : runId;
  const run = useScanRun(slug, attached);

  const start = useMutation({
    mutationFn: (k: Kind) =>
      projectApi(slug).requestScan(k === "describe" ? { trigger: "describe" } : { trigger: "manual" }),
    onSuccess: ({ run_id }, k) => {
      setKind(k);
      setRunId(run_id);
    },
    onError: (err) => toast.error(err.message),
  });

  // A finished run changes `last_scan_at` / `running_scan`: refresh both lists.
  useEffect(() => {
    if (run.finished) {
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
      void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "scan-estimate") });
    }
  }, [run.finished, queryClient, slug]);

  const openProject = () => void navigate({ to: "/p/$slug", params: { slug } });

  if (project.isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  const scanned = !!project.data?.last_scan_at;
  // A linked project's history is on the platform: nothing to describe, no cost to estimate.
  const linked = project.data?.source === "platform";

  // A run is in flight (or just finished).
  if (attached !== null && run.finished === null) {
    return (
      <Card title={kind === "describe" ? "Writing descriptions" : "First scan"}>
        <ScanProgress state={run} />
        {run.error && (
          <Alert variant="destructive">
            <AlertTitle>The scan hit a problem</AlertTitle>
            <AlertDescription>{run.error}</AlertDescription>
          </Alert>
        )}
      </Card>
    );
  }

  if (run.finished && run.finished !== "ok") {
    const message =
      run.error ??
      (typeof run.summary?.error === "string" ? run.summary.error : null) ??
      `The scan ${run.finished}.`;
    return (
      <Card title="Scan did not finish">
        <Alert variant="destructive" data-testid="scan-failed">
          <AlertTitle>Scan {run.finished}</AlertTitle>
          <AlertDescription>{message}</AlertDescription>
        </Alert>
        <div className="flex gap-2">
          <Button onClick={() => start.mutate(kind)} disabled={start.isPending}>
            Try again
          </Button>
          <Button variant="outline" onClick={openProject}>
            Open project anyway
          </Button>
        </div>
      </Card>
    );
  }

  if (run.finished === "ok" && kind === "describe") {
    return (
      <Card title="Descriptions written">
        <p className="text-sm text-muted-foreground">
          The scan finished and the commits now have descriptions.
        </p>
        <div>
          <Button onClick={openProject}>Open project</Button>
        </div>
      </Card>
    );
  }

  if ((run.finished === "ok" || scanned) && linked) {
    return (
      <Card title="First scan complete">
        <p className="text-sm text-muted-foreground">
          The code structure is indexed. Your agent now gets the platform's history for this project,
          placed against your own checkout.
        </p>
        <div>
          <Button onClick={openProject}>Open project</Button>
        </div>
      </Card>
    );
  }

  if (run.finished === "ok" || scanned) {
    return (
      <Card title="First scan complete">
        <p className="text-sm text-muted-foreground">
          Git history and code structure are indexed. Describing commits with an LLM is the only
          part that costs money, so it is your call:
        </p>
        <ScanEstimateCard
          slug={slug}
          busy={start.isPending}
          onDescribe={() => start.mutate("describe")}
          onLater={openProject}
        />
      </Card>
    );
  }

  return (
    <Card title="First scan">
      <p className="text-sm text-muted-foreground">
        {linked
          ? "The first scan indexes the code structure of this checkout. It makes no LLM calls and costs nothing."
          : "The first scan reads git history and indexes the code structure. It makes no LLM calls, so it costs nothing. You can explore while commit descriptions wait."}
      </p>
      <div>
        <Button onClick={() => start.mutate("initial")} disabled={start.isPending}>
          {start.isPending ? "Starting…" : "Start first scan"}
        </Button>
      </div>
    </Card>
  );
}
