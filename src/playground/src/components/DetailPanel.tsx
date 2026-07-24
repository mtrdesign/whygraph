import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { api } from "../api";
import { useExplorer } from "../store";
import { KindBadge, Spinner, EmptyState } from "../lib/ui";
import { RelationshipsTab } from "./RelationshipsTab";
import { RationaleTab } from "./RationaleTab";
import { EvidenceTab } from "./EvidenceTab";
import { HistoryTab } from "./HistoryTab";

type TabKey = "relationships" | "rationale" | "evidence" | "history";
const TABS: { key: TabKey; label: string }[] = [
  { key: "relationships", label: "Relationships" },
  { key: "rationale", label: "Rationale" },
  { key: "evidence", label: "Evidence" },
  { key: "history", label: "History" },
];

export function DetailPanel() {
  const selectedQn = useExplorer((s) => s.selectedQn);
  const [tab, setTab] = useState<TabKey>("relationships");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["node", selectedQn],
    queryFn: () => api.node(selectedQn!),
    enabled: !!selectedQn,
  });

  if (!selectedQn)
    return (
      <div className="flex h-full items-center justify-center p-4 text-center text-sm text-muted">
        Select a symbol to see its details.
      </div>
    );

  return (
    <div className="flex h-full flex-col">
      {/* Sticky identity header */}
      <div className="border-b border-border px-4 py-3">
        {isLoading && <Spinner label="Loading…" />}
        {isError && <div className="text-sm text-rose-400">{(error as Error).message}</div>}
        {data && (
          <>
            <div className="flex items-center gap-2">
              <KindBadge kind={data.symbol.kind} />
              <span className="truncate text-base font-semibold text-fg">
                {data.symbol.name}
              </span>
            </div>
            <div className="mt-1 truncate font-mono text-xs text-muted">
              {data.symbol.qualified_name}
            </div>
            <div className="truncate text-xs text-muted">
              {data.symbol.file_path}:{data.symbol.start_line}
            </div>
          </>
        )}
      </div>

      {/* Tabs */}
      <div className="flex border-b border-border">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={clsx(
              "flex-1 border-b-2 px-2 py-2 text-xs font-medium transition-colors",
              tab === t.key
                ? "border-accent2 text-fg"
                : "border-transparent text-muted hover:text-fg",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab body */}
      <div className="flex-1 overflow-auto">
        {!data ? (
          <EmptyState>…</EmptyState>
        ) : tab === "relationships" ? (
          <RelationshipsTab relations={data.relations} />
        ) : tab === "rationale" ? (
          <RationaleTab qualifiedName={selectedQn} />
        ) : tab === "evidence" ? (
          <EvidenceTab qualifiedName={selectedQn} />
        ) : (
          <HistoryTab path={data.symbol.file_path} />
        )}
      </div>
    </div>
  );
}
