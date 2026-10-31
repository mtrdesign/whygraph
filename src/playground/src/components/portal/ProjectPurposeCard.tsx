import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, projectApi, projectKey, type PurposeState } from "../../api";
import { errorMessage } from "../../lib/apiErrors";
import { providerLabel } from "../../lib/labels";
import { NOTE_BODY_MAX } from "../../lib/notes";
import { useCanFor } from "../../lib/permissions";
import { costPhrase, modelLabel } from "../../lib/scanFormat";
import { formatUsd } from "../../lib/format";
import { ErrorState } from "../state/ErrorState";
import { DisabledReason } from "../state/DisabledReason";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { Textarea } from "../ui/textarea";

const CARD = "flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card";

/** The query key of a project's purpose state. */
export const purposeKey = (slug: string) => projectKey(slug, "purpose");

/** The estimate's cost as "(about $0.01 on Anthropic, claude-haiku-4-5)", hidden for a caller without `project.usage`. */
export function purposeCostText(estimate: PurposeState["estimate"] | undefined): string {
  if (!estimate) return "";
  const model = estimate.model.provider ? modelLabel(providerLabel(estimate.model.provider), estimate.model.model) : null;
  const cost = estimate.cost && !estimate.cost_hidden ? costPhrase(estimate.cost.usd) : null;
  if (cost && model) return `${cost} on ${model}`;
  return cost ?? "";
}

/**
 * The project-purpose card: a short, confirmed statement of what the project is for.
 * States: none, running (survives a reload), proposed (Confirm / Edit), confirmed,
 * confirmed with a pending revision, failed, no key, and read-only. Never mount it for a
 * linked project: the route is refused there.
 */
export function ProjectPurposeCard({ slug }: { slug: string }) {
  const client = useQueryClient();
  const canConfirm = useCanFor(slug, "project.chat");
  const canEdit = useCanFor(slug, "project.configure");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const purpose = useQuery({
    queryKey: purposeKey(slug),
    queryFn: () => projectApi(slug).getPurpose(),
    retry: false,
    refetchInterval: (q) => (q.state.data?.running ? 3000 : false),
  });
  const generate = useMutation({
    mutationFn: () => projectApi(slug).generatePurpose(),
    onSuccess: (data) => client.setQueryData(purposeKey(slug), data),
    onSettled: () => void client.invalidateQueries({ queryKey: purposeKey(slug) }),
  });
  const confirm = useMutation({
    mutationFn: (v: { id: number; body?: string }) =>
      projectApi(slug).confirmNote(v.id, v.body !== undefined ? { body: v.body } : {}),
    onSuccess: () => {
      setEditing(false);
      void client.invalidateQueries({ queryKey: purposeKey(slug) });
      void client.invalidateQueries({ queryKey: projectKey(slug, "notes") });
    },
  });

  if (purpose.isLoading) return <Skeleton className="h-28 rounded-xl" />;
  if (purpose.isError || !purpose.data) {
    return (
      <section className={CARD} data-testid="purpose-card">
        <ErrorState error={purpose.error} title="Couldn't load the project purpose" onRetry={() => void purpose.refetch()} />
      </section>
    );
  }

  const { confirmed, proposed, running, last_run, stale, can_generate, estimate } = purpose.data;
  const busy = running || generate.isPending;
  const missingKey = estimate.missing_key;
  const costText = purposeCostText(estimate);
  const failed = !busy && !proposed && !!last_run?.error_code;
  const live = confirmed ?? proposed;
  const state = busy
    ? "running"
    : confirmed && proposed
      ? "revision"
      : confirmed
        ? "confirmed"
        : proposed
          ? "proposed"
          : failed
            ? "failed"
            : "none";

  const regenReason = busy
    ? "Already writing"
    : missingKey
      ? `Add the ${missingKey} key in the project's model settings first`
      : stale === false
        ? "Inputs unchanged since the last run"
        : undefined;
  const label = live ? "Regenerate" : "Describe the project's purpose";
  const generateButton = (
    <Button
      size="sm"
      variant={live ? "outline" : "default"}
      onClick={() => generate.mutate()}
      disabled={!!regenReason || generate.isPending}
    >
      {generate.isPending ? "Starting…" : label}
    </Button>
  );

  const startEdit = () => {
    setDraft(proposed?.body ?? "");
    setEditing(true);
  };

  return (
    <section className={CARD} data-testid="purpose-card" data-state={state} aria-labelledby="purpose-title">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 id="purpose-title" className="text-sm font-semibold">
            Project purpose
          </h2>
          <p className="text-xs text-muted-foreground">What this project is for, drafted from its README and manifests.</p>
        </div>
        {confirmed && <Badge variant="success">Confirmed</Badge>}
        {!confirmed && proposed && <Badge variant="brand">Waiting for review</Badge>}
      </div>

      {busy && (
        <p className="text-sm text-muted-foreground" data-testid="purpose-running">
          Writing the project's purpose…
        </p>
      )}

      {confirmed && (
        <p className="whitespace-pre-wrap text-sm" data-testid="purpose-confirmed">
          {confirmed.body}
        </p>
      )}

      {proposed && (
        <div className="flex flex-col gap-2" data-testid="purpose-proposed">
          {confirmed && <p className="text-xs font-medium">Pending revision</p>}
          {editing ? (
            <>
              <Textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                maxLength={NOTE_BODY_MAX}
                rows={6}
                aria-label="Project purpose"
              />
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  onClick={() => confirm.mutate({ id: proposed.id, body: draft.trim() })}
                  disabled={confirm.isPending || draft.trim() === ""}
                >
                  Save and confirm
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </div>
            </>
          ) : (
            <>
              <p className="whitespace-pre-wrap text-sm">{proposed.body}</p>
              {last_run && (
                <div className="text-xs text-muted-foreground" data-testid="purpose-run">
                  {last_run.model && <span>Model: {last_run.model}. </span>}
                  {last_run.cost_usd !== null && <span>Cost: {formatUsd(last_run.cost_usd)}. </span>}
                  {last_run.sources.length > 0 && (
                    <span>
                      Read: <span data-testid="purpose-sources">{last_run.sources.join(", ")}</span>
                    </span>
                  )}
                </div>
              )}
              {canConfirm && (
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" onClick={() => confirm.mutate({ id: proposed.id })} disabled={confirm.isPending}>
                    {confirm.isPending ? "Confirming…" : "Confirm"}
                  </Button>
                  {canEdit && (
                    <Button size="sm" variant="outline" onClick={startEdit}>
                      Edit
                    </Button>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {state === "none" && !missingKey && (
        <p className="text-sm text-muted-foreground" data-testid="purpose-none">
          No purpose statement yet.
        </p>
      )}
      {missingKey && !busy && (
        <p className="text-sm text-muted-foreground" data-testid="purpose-no-key">
          The {missingKey} key is not set, so the purpose cannot be written. Add it in the project's model settings.
        </p>
      )}
      {failed && last_run?.error_code && (
        <p className="text-sm text-destructive" role="alert" data-testid="purpose-failed">
          {errorMessage(new ApiError(422, "", last_run.error_code))}
        </p>
      )}
      {confirm.isError && <ErrorState error={confirm.error} size="inline" />}
      {generate.isError && <ErrorState error={generate.error} size="inline" />}

      {can_generate && !busy && (
        <div className="flex flex-wrap items-center gap-2">
          {regenReason ? <DisabledReason reason={regenReason}>{generateButton}</DisabledReason> : generateButton}
          {costText && !regenReason && (
            <span className="text-xs text-muted-foreground" data-testid="purpose-cost">
              ({costText})
            </span>
          )}
        </div>
      )}
    </section>
  );
}
