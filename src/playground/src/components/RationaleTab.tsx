import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type RationaleCard } from "../api";
import { Button, Spinner, EmptyState } from "../lib/ui";

// The Rationale tab (the resolved Q3 design): on open it does a CACHE-ONLY read
// (`GET`, never an LLM call). A cached card renders directly; otherwise a
// "Generate" button fires the `POST`, which runs the same MCP generation flow —
// so passive viewing never spends an LLM call, and the card can't drift.

function BulletList({ title, items }: { title: string; items?: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="mt-3">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted">
        {title}
      </div>
      <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-fg">
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
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted">
        Purpose
      </div>
      <p className="mt-1 text-sm text-fg">{card.purpose}</p>

      <div className="mt-3 text-[11px] font-semibold uppercase tracking-wide text-muted">
        Why it exists
      </div>
      <p className="mt-1 text-sm text-fg">{card.why}</p>

      <BulletList title="Constraints" items={card.constraints} />
      <BulletList title="Tradeoffs" items={card.tradeoffs} />
      <BulletList title="Risks" items={card.risks} />

      <div className="mt-4 border-t border-border pt-2 text-[11px] text-muted">
        {card.provider}
        {card.model ? ` · ${card.model}` : ""}
        {card.cached_at ? ` · generated ${card.cached_at}` : ""}
        {card.evidence_count &&
          ` · ${card.evidence_count.commits} commits, ${card.evidence_count.prs} PRs, ${card.evidence_count.issues} issues`}
      </div>
    </div>
  );
}

export function RationaleTab({ qualifiedName }: { qualifiedName: string }) {
  const queryClient = useQueryClient();
  const queryKey = ["rationale", qualifiedName];

  const { data, isLoading, isError, error } = useQuery({
    queryKey,
    queryFn: () => api.rationaleRead(qualifiedName),
  });

  const generate = useMutation({
    mutationFn: () => api.rationaleGenerate(qualifiedName),
    onSuccess: (card) => queryClient.setQueryData(queryKey, card),
  });

  if (isLoading) return <div className="p-4"><Spinner label="Checking cache…" /></div>;
  if (isError)
    return <EmptyState>Failed to load rationale: {(error as Error).message}</EmptyState>;

  if (data?.status === "cached") return <Card card={data} />;

  const noEvidence = data?.status === "no_evidence";

  return (
    <div className="p-4">
      {generate.isPending ? (
        <Spinner label="Generating rationale (calling the model)…" />
      ) : (
        <>
          <p className="text-sm text-muted">
            {noEvidence
              ? "No historical evidence maps to this symbol, so a rationale can't be generated. Run `whygraph scan` to populate history."
              : "No rationale has been generated for this symbol yet."}
          </p>
          <Button
            className="mt-3"
            disabled={noEvidence || generate.isPending}
            onClick={() => generate.mutate()}
          >
            Generate rationale
          </Button>
          {generate.isError && (
            <p className="mt-2 text-sm text-rose-400">
              {(generate.error as Error).message}
            </p>
          )}
        </>
      )}
    </div>
  );
}
