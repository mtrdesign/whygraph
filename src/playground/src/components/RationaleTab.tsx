import type { ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { portalApi, projectKey, type RationaleCard } from "../api";
import { isProduction, usePortalState } from "../lib/identity";
import { providerLabel } from "../lib/labels";
import { formatDateTime } from "../lib/format";
import { plural } from "../lib/plural";
import { useSlug } from "../lib/project";
import { budgetNoticeText } from "../lib/budgetBanner";
import { useLlmBlock, useProjectCan } from "../lib/permissions";
import { useProjectApi, useProjectKey, useProjectQuery } from "../lib/project";
import { Button } from "./ui/button";
import { ErrorState } from "./state/ErrorState";
import { Loading } from "./Loading";

/** "an Anthropic", "an OpenAI", "a DeepSeek": the article a provider's name takes. */
const withArticle = (name: string) => `${/^[aeiou]/i.test(name) ? "an" : "a"} ${name}`;

// The Rationale tab (the resolved Q3 design): on open it does a CACHE-ONLY read
// (`GET`, never an LLM call). A cached card renders directly; otherwise a
// "Generate" button fires the `POST`, which runs the same MCP generation flow —
// so passive viewing never spends an LLM call, and the card can't drift.

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

function Card({ card }: { card: RationaleCard }) {
  return (
    <div className="p-4">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Purpose
      </div>
      <p className="mt-1 text-sm text-foreground">{card.purpose}</p>

      <div className="mt-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Why it exists
      </div>
      <p className="mt-1 text-sm text-foreground">{card.why}</p>

      <BulletList title="Constraints" items={card.constraints} />
      <BulletList title="Tradeoffs" items={card.tradeoffs} />
      <BulletList title="Risks" items={card.risks} />

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
function RescanHint({ production }: { production: boolean }) {
  return (
    <p className="mt-2 text-xs text-muted-foreground" data-testid="rationale-scan-hint">
      Rescan after new commits from the project Overview
      {production ? "." : (
        <>
          {" "}
          (or run <code className="font-mono text-foreground">whygraph scan</code> outside the portal).
        </>
      )}
    </p>
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
  const production = isProduction(usePortalState().data);
  const llm = useLlmBlock();
  // A hard-stopped budget: the button stays, disabled, with the reason beside it.
  const budgetBlocked = llm.block === "budget_exceeded";
  const missingKey = project.data?.missing_key ?? null;
  const generate = useMutation({
    mutationFn: () => api.rationaleGenerate(qualifiedName),
    onSuccess: (card) => queryClient.setQueryData(queryKey, card),
  });

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

  if (data?.status === "cached") return <Card card={data} />;
  if (generate.isPending)
    return (
      <div className="p-4">
        <Loading label="Generating rationale (calling the model)…" />
      </div>
    );

  // One state at a time (EXC-1), in this order: no history, a viewer, a hard stop, no key, ready.
  let state: ReactNode;
  if (data?.status === "no_evidence") {
    state = (
      <>
        <p className="text-sm text-muted-foreground" data-testid="rationale-no-evidence">
          No history to explain yet: this symbol has no commits in the scanned history.
        </p>
        <RescanHint production={production} />
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
    <div className="p-4">
      {state}
      {generate.isError && (
        <ErrorState className="mt-3" size="inline" context="chat" error={generate.error} />
      )}
    </div>
  );
}
