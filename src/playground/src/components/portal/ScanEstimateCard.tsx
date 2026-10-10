import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { projectApi, projectKey, type ScanEstimate } from "../../api";
import { useCanFor } from "../../lib/permissions";
import { ErrorState } from "../state/ErrorState";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { formatTokens, formatUsd } from "../../lib/format";
import { providerLabel } from "../../lib/labels";
import { plural } from "../../lib/plural";

/** `prices_as_of` is the bundled table's date, or a label when the org's own prices applied. */
function pricedWith(asOf: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(asOf) ? `prices as of ${asOf}` : `priced with ${asOf}`;
}

/**
 * The §4.14 cost guard: what describing the waiting commits would cost, with
 * *Describe now* / *Later*. The count is an upper bound (empty and PR-only commits
 * stay undescribed); a model outside the price table shows tokens only; a missing
 * provider key disables *Describe now* and links to the project's model settings.
 * A caller without `project.usage` gets the count without a price (R6,
 * `cost_hidden`).
 */
export function ScanEstimateCard({
  slug,
  onDescribe,
  onLater,
  busy = false,
  actions = true,
  totalCommits,
}: {
  slug: string;
  onDescribe: () => void;
  onLater: () => void;
  busy?: boolean;
  /** `false` leaves the choices to the caller (the wizard's footer). */
  actions?: boolean;
  /** Every commit the project has, when known: 0 says "No commits yet" (BUG-14). */
  totalCommits?: number | null;
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
    return <ErrorState error={estimate.error} title="Couldn't estimate the cost" onRetry={() => void estimate.refetch()} />;
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
      actions={actions}
      totalCommits={totalCommits}
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
  actions = true,
  totalCommits,
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
  /** `false` hides *Describe now* / *Later* (the caller offers its own). */
  actions?: boolean;
  /** Every commit the project has, when known: 0 says "No commits yet" (BUG-14). */
  totalCommits?: number | null;
}) {
  const { commits, model, tokens, cost, large_commits, missing_key, cost_hidden } = estimate;
  const modelName = model.model ? `${model.provider}/${model.model}` : (model.provider ?? "default model");
  const showActions = actions && commits > 0;
  return (
    <div className="flex flex-col gap-4" data-testid="scan-estimate">
      {commits === 0 ? (
        <p className="text-sm">
          {totalCommits === 0
            ? "No commits yet. Push some history, then rescan."
            : "Nothing to describe: every reachable commit already has a description."}
        </p>
      ) : cost_hidden ? (
        // R6: no price (and no tokens, which would give it back) without project.usage.
        <p className="text-sm">
          {plural(commits, "commit")} {commits === 1 ? "is" : "are"} waiting for descriptions.
          {!canDescribe && " Ask a project admin to describe them."}
        </p>
      ) : (
        <div className="flex flex-col gap-1">
          <p className="text-sm font-medium">
            {plural(commits, "commit")} to describe{" "}
            <span className="font-normal text-muted-foreground">(upper bound)</span>
          </p>
          <p className="text-sm text-muted-foreground">
            Model <span className="font-mono text-foreground">{modelName}</span>
            {tokens && (
              <>
                {" "}
                · about{" "}
                <span data-testid="estimate-tokens">
                  {formatTokens(tokens.input)} input and {formatTokens(tokens.output)} output tokens
                </span>
              </>
            )}
          </p>
          {cost ? (
            <p className="text-sm text-muted-foreground" data-testid="estimate-cost">
              About <span className="font-medium text-foreground">~{formatUsd(cost.usd)}</span>, between{" "}
              {formatUsd(cost.low)} and {formatUsd(cost.high)} ({pricedWith(cost.prices_as_of)})
            </p>
          ) : (
            tokens && <p className="text-sm text-muted-foreground">No price on file for this model, so tokens only.</p>
          )}
          {large_commits > 0 && (
            <p className="text-xs text-muted-foreground">
              {plural(large_commits, "very large commit")} {large_commits === 1 ? "is" : "are"} described file by
              file on demand, not now.
            </p>
          )}
        </div>
      )}
      {commits > 0 && missing_key && (
        <div className="flex flex-wrap items-center gap-2" data-testid="missing-key">
          <Badge variant="info">No {providerLabel(missing_key)} key yet</Badge>
          {canConfigure && (
            <Link
              to="/p/$slug/settings"
              params={{ slug }}
              search={{ section: "models" }}
              className="text-sm text-primary-text hover:underline"
            >
              Add a key in Settings
            </Link>
          )}
        </div>
      )}
      {showActions && (canDescribe || !cost_hidden) && (
        <div className="flex items-center gap-2">
          {canDescribe && (
            <Button onClick={onDescribe} disabled={!!missing_key || busy}>
              Describe now
            </Button>
          )}
          <Button variant="outline" onClick={onLater} disabled={busy}>
            Later
          </Button>
        </div>
      )}
      {actions && commits === 0 && (
        <div>
          <Button onClick={onLater} disabled={busy}>
            Open project
          </Button>
        </div>
      )}
      {showActions && canDescribe && (
        <p className="text-xs text-muted-foreground">
          <strong className="font-medium">Later</strong> leaves descriptions to on-demand
          generation as you browse, and to later full scans. Nothing is spent until then.
        </p>
      )}
    </div>
  );
}
