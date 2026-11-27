import type { ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  portalApi,
  projectKey,
  type PlanRef,
  type RationaleCard,
  type RationaleItem,
  type RationaleItemRef,
} from "../api";
import { providerLabel } from "../lib/labels";
import { depthShortName, formatCostRange } from "../lib/rationaleDepth";
import { formatDateTime } from "../lib/format";
import { plural } from "../lib/plural";
import { useSlug } from "../lib/project";
import { budgetNoticeText } from "../lib/budgetBanner";
import { useLlmBlock, useProjectCan } from "../lib/permissions";
import { useProjectApi, useProjectKey, useProjectQuery } from "../lib/project";
import { Badge } from "./ui/badge";
import { DepthBadge } from "./portal/DepthInfo";
import { Button } from "./ui/button";
import { ErrorState } from "./state/ErrorState";
import { Loading } from "./Loading";

/** "an Anthropic", "an OpenAI", "a DeepSeek": the article a provider's name takes. */
const withArticle = (name: string) => `${/^[aeiou]/i.test(name) ? "an" : "a"} ${name}`;

// The Rationale tab (the resolved Q3 design): on open it does a CACHE-ONLY read
// (`GET`, never an LLM call). A cached card renders directly; otherwise a
// "Generate" button fires the `POST`, which runs the same MCP generation flow —
// so passive viewing never spends an LLM call, and the card can't drift.

const refLabel = (r: RationaleItemRef) =>
  r.kind === "commit" ? r.id.slice(0, 8) : `${r.kind === "pr" ? "PR" : "issue"} #${r.id}`;

function ItemRef({ r }: { r: RationaleItemRef }) {
  if (r.url)
    return (
      <a
        href={r.url}
        target="_blank"
        rel="noreferrer"
        className="text-primary-text underline-offset-4 hover:underline"
        data-testid="rationale-ref"
      >
        {refLabel(r)}
      </a>
    );
  return (
    <span className={r.kind === "commit" ? "font-mono" : undefined} data-testid="rationale-ref">
      {refLabel(r)}
    </span>
  );
}

/** A constraint or risk: its text, a basis tag (none for an old card) and its sources. */
function ItemRow({ item }: { item: RationaleItem }) {
  const refs = item.refs ?? [];
  const tagged = item.basis !== "unknown" && item.basis !== undefined;
  return (
    <li>
      {item.text}
      {tagged && (
        <span
          className="ml-2 rounded border border-border px-1.5 py-0.5 align-middle text-[10px] uppercase tracking-wide text-muted-foreground"
          data-testid="rationale-basis"
          title={item.basis === "quoted" && item.quote ? item.quote : undefined}
        >
          {item.basis}
        </span>
      )}
      {item.check && item.check.verdict !== "holds" && (
        <span
          className={
            item.check.verdict === "contradicted"
              ? "ml-2 text-xs text-destructive"
              : "ml-2 text-xs text-muted-foreground"
          }
          data-testid="rationale-check"
          title={item.check.reason || undefined}
        >
          {item.check.verdict === "contradicted" ? "Contradicted by today's code" : "Could not be checked"}
        </span>
      )}
      {refs.length > 0 && (
        <span className="ml-2 text-xs text-muted-foreground">
          {refs.map((r, i) => (
            <span key={`${r.kind}-${r.id}`}>
              {i > 0 && ", "}
              <ItemRef r={r} />
            </span>
          ))}
        </span>
      )}
    </li>
  );
}

function ItemList({ title, items }: { title: string; items?: RationaleItem[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="mt-3">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-foreground">
        {items.map((item, i) => (
          <ItemRow key={i} item={item} />
        ))}
      </ul>
    </div>
  );
}

function BulletList({ title, items }: { title: string; items?: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="mt-3">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-foreground">
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

/** A partial card: what it lacked, a Complete button, and what happened after the last click. */
function PartialNote({
  card,
  canComplete,
  pending,
  outcome,
  onComplete,
}: {
  card: RationaleCard;
  canComplete: boolean;
  pending: boolean;
  outcome: RationaleCard | undefined;
  onComplete: () => void;
}) {
  const n = card.describing ?? 0;
  return (
    <div className="mb-3 rounded-md border border-border bg-muted/40 p-3" data-testid="rationale-partial">
      <p className="text-sm text-foreground">
        Built while {plural(n, "commit")} still had no description
      </p>
      {canComplete && (
        <Button className="mt-2" size="sm" variant="outline" disabled={pending} onClick={onComplete}>
          {pending ? "Completing…" : "Complete"}
        </Button>
      )}
      {outcome && (
        <p className="mt-2 text-xs text-muted-foreground" role="status" data-testid="rationale-complete-result">
          {outcome.completing
            ? "Completing in the background; reopen this tab in a few minutes"
            : "Cannot complete it right now; try again later"}
        </p>
      )}
    </div>
  );
}

const VERIFICATION_NOTE = {
  failed: "The verification pass failed, so the claims are not checked against today's code.",
  skipped: "The verification pass was skipped, so the claims are not checked against today's code.",
  pending: "The claims will be checked against today's code once the missing descriptions are in.",
} as const;

/** What this card was built from: the level and why, what it read, and what it could not check. */
function BuiltNotes({ card, deeper }: { card: RationaleCard; deeper?: ReactNode }) {
  const level = card.depth?.resolved;
  const c = card.coverage;
  const context = (c?.context_commits ?? 0) + (c?.context_omitted ?? 0);
  const lines: ReactNode[] = [];
  if (level) lines.push(card.depth?.reason || `Built at ${depthShortName(level)}.`);
  if (c?.line_commits !== undefined)
    lines.push(
      `Read ${plural(c.line_commits, "commit")} that wrote these lines${
        context > 0 ? `, ${c.context_commits ?? 0} of ${context} other commits to the file` : ""
      }.`,
    );
  if (c?.verification && c.verification in VERIFICATION_NOTE)
    lines.push(VERIFICATION_NOTE[c.verification as keyof typeof VERIFICATION_NOTE]);
  if (card.context_stale) lines.push("Newer commits to this file are not reflected.");
  if ((c?.descriptions_missing ?? 0) > 0 && card.partial !== true)
    lines.push(`${plural(c!.descriptions_missing!, "commit")} had no description when this card was built.`);
  if (card.deeper)
    lines.push(
      `A ${depthShortName(card.deeper.level)} card ${card.deeper.adds.charAt(0).toLowerCase()}${card.deeper.adds.slice(1)}`,
    );
  if (lines.length === 0 && !deeper) return null;
  return (
    <div className="mt-4 flex flex-col gap-1 text-[13px] text-muted-foreground" data-testid="rationale-built">
      {lines.map((line, i) => (
        <p key={i}>{line}</p>
      ))}
      {deeper}
    </div>
  );
}

function Card({ card, note, deeper }: { card: RationaleCard; note?: ReactNode; deeper?: ReactNode }) {
  return (
    <div className="p-4">
      {card.depth && (
        <div className="mb-3 flex justify-end">
          <DepthBadge level={card.depth.resolved} />
        </div>
      )}
      {note}
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Purpose
      </div>
      <p className="mt-1 text-sm text-foreground">{card.purpose}</p>

      <div className="mt-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Why it exists
      </div>
      <p className="mt-1 text-sm text-foreground">{card.why}</p>

      <ItemList title="Constraints" items={card.constraints} />
      <BulletList title="Tradeoffs" items={card.tradeoffs} />
      <ItemList title="Risks" items={card.risks} />

      <BuiltNotes card={card} deeper={deeper} />

      <div className="mt-4 border-t border-border pt-2 text-[11px] text-muted-foreground">
        {card.provider}
        {card.model ? ` · ${card.model}` : ""}
        {card.cached_at ? ` · generated ${formatDateTime(card.cached_at)}` : ""}
        {card.evidence_count &&
          ` · ${plural(card.evidence_count.commits, "commit")}, ${plural(card.evidence_count.prs, "PR")}, ${plural(card.evidence_count.issues, "issue")}`}
      </div>
    </div>
  );
}

/** The one line under "no rationale yet": what to do to get history in. */
function RescanHint() {
  return (
    <p className="mt-2 text-xs text-muted-foreground" data-testid="rationale-scan-hint">
      Rescan after new commits from the project Overview.
    </p>
  );
}

const STATUS_LABEL = { shipped: "Shipped", in_flight: "In flight", not_implemented: "Not implemented" } as const;
const STATUS_VARIANT = { shipped: "success", in_flight: "info", not_implemented: "outline" } as const;
const LINK_PHRASE: Record<string, string> = {
  commit: "built by commits that name this plan",
  pr: "built in a PR that names this plan",
  declared: "this plan declares a change to this code",
};
const LINK_CLASS = "text-primary-text underline-offset-4 hover:underline";

/** The plans linked to the symbol: links into the Plans page, never plan text (R19, R22). */
function PlansBlock({ plans }: { plans?: PlanRef[] }) {
  const slug = useSlug();
  // A `file` link never reaches this block.
  const rows = (plans ?? []).filter((p) => p.link !== "file");
  if (rows.length === 0) return null;
  return (
    <div className="border-t border-border p-4" data-testid="related-plans">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Related plans</div>
      <ul className="mt-2 flex flex-col gap-2 text-sm">
        {rows.map((p) => (
          <li key={p.stem} data-testid={`related-plan-${p.stem}`}>
            <div className="flex flex-wrap items-center gap-2">
              <Link to="/p/$slug/plans" params={{ slug }} search={{ stem: p.stem }} className={LINK_CLASS}>
                {p.title}
              </Link>
              <Badge variant={STATUS_VARIANT[p.status]}>{STATUS_LABEL[p.status]}</Badge>
              {p.present === false && (
                <span className="text-[13px] text-muted-foreground">No longer in the repository</span>
              )}
            </div>
            {p.link && LINK_PHRASE[p.link] && (
              <div className="text-[13px] text-muted-foreground">{LINK_PHRASE[p.link]}</div>
            )}
            {p.hints && p.hints.length > 0 && (
              <div className="text-[13px] text-muted-foreground">
                See:{" "}
                {p.hints.slice(0, 2).map((h, i) => (
                  <span key={`${h.id}-${h.n}`}>
                    {i > 0 && ", "}
                    <Link
                      to="/p/$slug/plans"
                      params={{ slug }}
                      search={{ stem: p.stem, section: h.n }}
                      className={LINK_CLASS}
                    >
                      {h.title ? `${h.id}: ${h.title}` : h.id}
                    </Link>
                  </span>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>
      <p className="mt-2 text-[13px] text-muted-foreground">Plans state what was intended, not what was built.</p>
    </div>
  );
}

export function RationaleTab({ qualifiedName }: { qualifiedName: string }) {
  const queryClient = useQueryClient();
  const api = useProjectApi();
  const slug = useSlug();
  const queryKey = useProjectKey()("rationale", qualifiedName);

  const { data, isLoading, isError, error, refetch } = useProjectQuery(["rationale", qualifiedName], (api) =>
    api.rationaleRead(qualifiedName),
  );

  const canGenerate = useProjectCan("project.chat");
  const canConfigure = useProjectCan("project.configure");
  const project = useQuery({ queryKey: projectKey(slug, "project"), queryFn: () => portalApi.project(slug) });
  const llm = useLlmBlock();
  // A hard-stopped budget: the button stays, disabled, with the reason beside it.
  const budgetBlocked = llm.block === "budget_exceeded";
  const missingKey = project.data?.missing_key ?? null;
  const generate = useMutation({
    mutationFn: () => api.rationaleGenerate(qualifiedName),
    onSuccess: (card) => queryClient.setQueryData(queryKey, card),
  });

  // "Generate deeper" (R41): the next level through the same POST, gated like Generate, never above the ceiling.
  const deeperLevel = data?.status === "cached" ? (data.deeper?.level ?? null) : null;
  const canDeepen = deeperLevel !== null && canGenerate && !budgetBlocked && !missingKey;
  const deepen = useMutation({
    mutationFn: (level: string) => api.rationaleGenerate(qualifiedName, level),
    onSuccess: (card) => queryClient.setQueryData(queryKey, card),
  });
  const profile = useProjectQuery(["depth-profile"], (api) => api.depthProfile(), {
    enabled: canDeepen,
    staleTime: 60_000,
    refetchInterval: (query) => (query.state.data?.status === "computing" ? 1500 : false),
  });
  const deeperPrice =
    deeperLevel && profile.data?.status === "ready" && !profile.data.cost_hidden
      ? formatCostRange(profile.data.levels[deeperLevel as "medium" | "high"]?.cost)
      : null;

  if (isLoading) return <div className="p-4"><Loading label="Checking cache…" /></div>;
  if (isError)
    return (
      <ErrorState
        className="p-3"
        title="Couldn't load the rationale"
        error={error}
        onRetry={() => void refetch()}
      />
    );

  if (data?.status === "cached")
    return (
      <>
      <Card
        card={data}
        deeper={
          canDeepen && deeperLevel ? (
            <>
              <Button
                className="mt-2 self-start"
                size="sm"
                variant="outline"
                disabled={deepen.isPending}
                data-testid="generate-deeper"
                onClick={() => deepen.mutate(deeperLevel)}
              >
                {deepen.isPending
                  ? "Generating…"
                  : `Generate deeper - ${depthShortName(deeperLevel)}${deeperPrice ? ` · about ${deeperPrice}` : ""}`}
              </Button>
              {deepen.isError && <ErrorState className="mt-1" size="inline" context="chat" error={deepen.error} />}
            </>
          ) : undefined
        }
        note={
          data.partial === true ? (
            <PartialNote
              card={data}
              canComplete={canGenerate && !budgetBlocked && !missingKey}
              pending={generate.isPending}
              outcome={generate.data}
              onComplete={() => generate.mutate()}
            />
          ) : undefined
        }
      />
      <PlansBlock plans={data.plans} />
      </>
    );
  if (generate.isPending)
    return (
      <>
        <div className="p-4">
          <Loading label="Generating rationale (calling the model)…" />
        </div>
        <PlansBlock plans={data?.plans} />
      </>
    );

  // One state at a time (EXC-1), in this order: no history, a viewer, a hard stop, no key, ready.
  let state: ReactNode;
  if (data?.status === "no_evidence") {
    state = (
      <>
        <p className="text-sm text-muted-foreground" data-testid="rationale-no-evidence">
          No history to explain yet: this symbol has no commits in the scanned history.
        </p>
        <RescanHint />
      </>
    );
  } else if (!canGenerate) {
    state = (
      <p className="text-sm text-muted-foreground" data-testid="rationale-viewer">
        Viewers can read cards but not generate them.
      </p>
    );
  } else if (budgetBlocked) {
    // One reason, once (USE-3): the page's banner says why and who can change it.
    state = (
      <>
        <Button className="mt-0" disabled title={budgetNoticeText(llm.scope)}>
          Generate rationale
        </Button>
        <p className="mt-2 text-sm text-muted-foreground" data-testid="generate-blocked">
          Generation is paused: monthly budget reached
        </p>
      </>
    );
  } else if (missingKey) {
    state = (
      <p className="text-sm text-muted-foreground" data-testid="rationale-no-key">
        Add {withArticle(providerLabel(missingKey))} key to generate rationale.{" "}
        {canConfigure ? (
          <Link
            to="/p/$slug/settings"
            params={{ slug }}
            search={{ section: "models" }}
            className="text-primary-text underline-offset-4 hover:underline"
          >
            Open Settings
          </Link>
        ) : (
          "Ask a project admin to add a key."
        )}
      </p>
    );
  } else {
    state = (
      <>
        <p className="text-sm text-muted-foreground">No rationale has been generated for this symbol yet.</p>
        <Button className="mt-3" onClick={() => generate.mutate()}>
          Generate rationale
        </Button>
      </>
    );
  }

  return (
    <>
      <div className="p-4">
        {state}
        {generate.isError && (
          <ErrorState className="mt-3" size="inline" context="chat" error={generate.error} />
        )}
      </div>
      <PlansBlock plans={data?.plans} />
    </>
  );
}
