import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { projectApi, projectKey, type DescribeScope, type ScanEstimate } from "../../api";
import { useCanFor } from "../../lib/permissions";
import { ErrorState } from "../state/ErrorState";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { formatDate, formatTokens } from "../../lib/format";
import { costPhrase, estimateCostLine, modelLabel } from "../../lib/scanFormat";
import { coreEstimateReason } from "../../lib/scanAvailability";
import { DisabledReason } from "../state/DisabledReason";
import { providerLabel } from "../../lib/labels";
import { plural } from "../../lib/plural";

/** `prices_as_of` is the bundled table's date, or a label when the org's own prices applied. */
function pricedWith(asOf: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(asOf) ? `prices as of ${asOf}` : `priced with ${asOf}`;
}

/**
 * The Describe core scope for the *Also describe large commits file by file* choice:
 * unticked is the cheapest run, `core_commits`.
 */
export function coreScope(withSlices: boolean): DescribeScope {
  return withSlices ? "core" : "core_commits";
}

/** The core figure Describe core would spend with or without the per-file slices. */
export function coreFigure(core: NonNullable<ScanEstimate["core"]>, withSlices: boolean) {
  return withSlices || !core.commits_only ? core : core.commits_only;
}

/**
 * The §4.14 cost guard: what describing the waiting commits would cost, with
 * *Describe core* / *Describe all* / *Later*. The count is an upper bound (empty and PR-only commits
 * stay undescribed); a model outside the price table shows tokens only; a missing
 * provider key disables both describe buttons and links to the project's model settings.
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
  withSlices,
  onWithSlices,
}: {
  slug: string;
  onDescribe: (scope: DescribeScope) => void;
  onLater: () => void;
  busy?: boolean;
  /** `false` leaves the choices to the caller (the wizard's footer). */
  actions?: boolean;
  /** Every commit the project has, when known: 0 says "No commits yet" (BUG-14). */
  totalCommits?: number | null;
  /** The file-slices choice, when the caller owns it (the wizard's footer button reads it). */
  withSlices?: boolean;
  onWithSlices?: (v: boolean) => void;
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
      withSlices={withSlices}
      onWithSlices={onWithSlices}
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
  withSlices: controlled,
  onWithSlices,
}: {
  slug: string;
  estimate: ScanEstimate;
  /** `project.scan_full`: Describe now spends LLM tokens. */
  canDescribe: boolean;
  /** `project.configure`: the link to add a key. */
  canConfigure: boolean;
  onDescribe: (scope: DescribeScope) => void;
  onLater: () => void;
  busy?: boolean;
  /** `false` hides *Describe now* / *Later* (the caller offers its own). */
  actions?: boolean;
  /** Every commit the project has, when known: 0 says "No commits yet" (BUG-14). */
  totalCommits?: number | null;
  /** *Also describe large commits file by file*; ticked unless the caller says otherwise. */
  withSlices?: boolean;
  onWithSlices?: (v: boolean) => void;
}) {
  const [own, setOwn] = useState(true);
  const sliced = controlled ?? own;
  const setSliced = onWithSlices ?? setOwn;
  const { commits, model, tokens, cost, large_commits, missing_key, cost_hidden } = estimate;
  const modelName = model.provider ? modelLabel(providerLabel(model.provider), model.model) : "The default model";
  const slices = estimate.file_slices ?? 0;
  const core = estimate.core ?? null;
  const coreReason = coreEstimateReason(estimate, sliced);
  const coreWork = !!core && (core.commits + core.file_slices > 0 || core.stale);
  const anyWork = commits > 0 || coreWork;
  const showActions = actions && anyWork;
  const withSlices = (n: number) => (n > 0 ? ` + ${plural(n, "file change")}` : "");
  const price = (c: ScanEstimate["cost"] | undefined) => (c && !cost_hidden ? ` (${costPhrase(c.usd)})` : "");
  const allLine = `All: ${plural(commits, "commit")}${withSlices(slices)}${price(cost)}`;
  const coreChosen = core ? coreFigure(core, sliced) : null;
  const coreLine =
    core && coreChosen
      ? `Core: ${plural(core.commits, "commit")} in ${plural(core.files, "file")}${withSlices(coreChosen.file_slices)}${price(coreChosen.cost)}` +
        ` · as of ${formatDate(core.as_of)}` +
        (core.stale ? " · may have new commits" : "")
      : null;
  // The choice only matters when Describe core has slices to describe and a cheaper figure exists.
  const slicesChoice = !!core?.commits_only && core.file_slices > 0 && canDescribe;
  const extra = core?.cost && core.commits_only?.cost && !cost_hidden ? core.cost.usd - core.commits_only.cost.usd : null;
  return (
    <div className="flex flex-col gap-4" data-testid="scan-estimate">
      {commits === 0 && coreWork ? (
        <p className="text-sm">No commits are waiting for descriptions in the whole history.</p>
      ) : commits === 0 ? (
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
            Model: <span className="text-foreground">{modelName}</span>
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
              <span className="font-medium text-foreground">{estimateCostLine(cost)}</span>, {pricedWith(cost.prices_as_of)}
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
      {anyWork && (
        <div className="flex flex-col gap-0.5 text-sm" data-testid="estimate-scopes">
          {coreLine && <p data-testid="estimate-core">{coreLine}</p>}
          {slicesChoice && core && (
            <label className="flex items-start gap-2 pl-3 text-xs text-muted-foreground" data-testid="estimate-core-slices">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={sliced}
                onChange={(e) => setSliced(e.target.checked)}
              />
              <span>
                Also describe large commits file by file ({plural(core.file_slices, "file change")}
                {extra != null ? `, ${costPhrase(extra)} more` : ""}). Unticked, they are described when a question reaches
                them.
              </span>
            </label>
          )}
          <p data-testid="estimate-all">{allLine}</p>
          {core?.selection === "all_text" && (
            <p className="text-xs text-muted-foreground">No usable code index, so core covers every text file.</p>
          )}
        </div>
      )}
      {anyWork && missing_key && (
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
        <div className="flex flex-wrap items-center gap-2">
          {canDescribe && (
            <>
              {(() => {
                const reason = coreReason;
                const button = (
                  <Button
                    onClick={() => onDescribe(coreScope(sliced))}
                    disabled={!!missing_key || busy || !!reason}
                    title="commits behind the files the rest of the code depends on"
                  >
                    Describe core
                  </Button>
                );
                return reason ? <DisabledReason reason={reason}>{button}</DisabledReason> : button;
              })()}
              <Button
                variant="outline"
                onClick={() => onDescribe("all")}
                disabled={!!missing_key || busy || (commits === 0 && slices === 0)}
              >
                Describe all
              </Button>
            </>
          )}
          <Button variant="outline" onClick={onLater} disabled={busy}>
            Later
          </Button>
        </div>
      )}
      {actions && !anyWork && (
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
      {showActions && canDescribe && (
        <p className="text-xs text-muted-foreground" data-testid="core-help">
          Core covers the commits behind the files the rest of your code depends on. It refreshes first, so the count
          can change a little. Other commits are described when a question reaches them, and are not searchable by
          description until then.
        </p>
      )}
    </div>
  );
}
