import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { projectApi, projectKey, type ScanEstimate } from "../../api";
import { useCanFor } from "../../lib/permissions";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";

/** `1_234_567` -> `1.2M`; small numbers stay exact. */
export function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(Math.round(n));
}

const usd = (n: number) => (n < 10 ? `$${n.toFixed(2)}` : `$${Math.round(n).toLocaleString("en-US")}`);

/**
 * The §4.14 cost guard: what describing the waiting commits would cost, with
 * *Describe now* / *Later*. The count is an upper bound (empty and PR-only commits
 * stay undescribed); a model outside the price table shows tokens only; a missing
 * provider key disables *Describe now* and links to Configure.
 */
export function ScanEstimateCard({
  slug,
  onDescribe,
  onLater,
  busy = false,
}: {
  slug: string;
  onDescribe: () => void;
  onLater: () => void;
  busy?: boolean;
}) {
  const canDescribe = useCanFor(slug, "project.scan_full");
  const canConfigure = useCanFor(slug, "project.configure");
  const estimate = useQuery({
    queryKey: projectKey(slug, "scan-estimate"),
    queryFn: () => projectApi(slug).scanEstimate(),
    retry: false,
  });

  if (estimate.isLoading) return <p className="text-sm text-muted-foreground">Estimating the cost…</p>;
  if (estimate.isError || !estimate.data) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not estimate the cost</AlertTitle>
        <AlertDescription>{estimate.error?.message}</AlertDescription>
      </Alert>
    );
  }
  return (
    <EstimateBody
      slug={slug}
      estimate={estimate.data}
      canDescribe={canDescribe}
      canConfigure={canConfigure}
      onDescribe={onDescribe}
      onLater={onLater}
      busy={busy}
    />
  );
}

export function EstimateBody({
  slug,
  estimate,
  canDescribe,
  canConfigure,
  onDescribe,
  onLater,
  busy = false,
}: {
  slug: string;
  estimate: ScanEstimate;
  /** `project.scan_full`: Describe now spends LLM tokens. */
  canDescribe: boolean;
  /** `project.configure`: the link to add a key. */
  canConfigure: boolean;
  onDescribe: () => void;
  onLater: () => void;
  busy?: boolean;
}) {
  const { commits, model, tokens, cost, large_commits, missing_key } = estimate;
  const modelName = model.model ? `${model.provider}/${model.model}` : (model.provider ?? "default model");
  return (
    <div className="flex flex-col gap-4" data-testid="scan-estimate">
      {commits === 0 ? (
        <p className="text-sm">
          Nothing to describe: every reachable commit already has a description.
        </p>
      ) : (
        <>
          <div>
            <p className="text-sm font-medium">
              {commits.toLocaleString("en-US")} commits to describe{" "}
              <span className="font-normal text-muted-foreground">(upper bound)</span>
            </p>
            <p className="mt-1 text-sm text-muted-foreground">
              Model <span className="font-mono text-foreground">{modelName}</span> · about{" "}
              <span data-testid="estimate-tokens">
                {compact(tokens.input)} input and {compact(tokens.output)} output tokens
              </span>
              {cost ? (
                <>
                  {" "}
                  · <span className="font-medium text-foreground">~{usd(cost.usd)}</span>{" "}
                  <span className="text-xs">
                    (range {usd(cost.low)} to {usd(cost.high)}, prices as of {cost.prices_as_of})
                  </span>
                </>
              ) : (
                <> · no price on file for this model, so tokens only</>
              )}
            </p>
            {large_commits > 0 && (
              <p className="mt-1 text-xs text-muted-foreground">
                {large_commits} very large commits are described file by file on demand, not now.
              </p>
            )}
          </div>
          {missing_key && (
            <div className="flex flex-wrap items-center gap-2" data-testid="missing-key">
              <Badge variant="destructive">no key for {missing_key}</Badge>
              {canConfigure && (
                <Link
                  to="/p/$slug/init"
                  params={{ slug }}
                  search={{ step: "configure" }}
                  className="text-sm text-primary-text hover:underline"
                >
                  Add a key in Configure
                </Link>
              )}
            </div>
          )}
        </>
      )}
      <div className="flex items-center gap-2">
        {commits > 0 && canDescribe && (
          <Button onClick={onDescribe} disabled={!!missing_key || busy}>
            Describe now
          </Button>
        )}
        <Button variant={commits > 0 ? "outline" : "default"} onClick={onLater} disabled={busy}>
          {commits > 0 ? "Later" : "Open project"}
        </Button>
      </div>
      {commits > 0 && canDescribe && (
        <p className="text-xs text-muted-foreground">
          <strong className="font-medium">Later</strong> leaves descriptions to on-demand
          generation as you browse, and to later full scans. Nothing is spent until then.
        </p>
      )}
    </div>
  );
}
