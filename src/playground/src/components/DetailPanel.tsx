import { useState } from "react";
import { useExplorerSearch } from "../lib/nav";
import { useProjectQuery } from "../lib/project";
import { KindBadge } from "./KindBadge";
import { Loading } from "./Loading";
import { Empty, EmptyDescription } from "./ui/empty";
import { ScrollArea } from "./ui/scroll-area";
import { Skeleton } from "./ui/skeleton";
import { ErrorState } from "./state/ErrorState";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";
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
  const selectedQn = useExplorerSearch().node;
  const [tab, setTab] = useState<TabKey>("relationships");

  const { data, isLoading, isError, error, refetch } = useProjectQuery(
    ["node", selectedQn],
    (api) => api.node(selectedQn!),
    { enabled: !!selectedQn },
  );

  if (!selectedQn)
    return (
      <Empty className="h-full">
        <EmptyDescription>Select a symbol to see its details.</EmptyDescription>
      </Empty>
    );

  return (
    <div className="flex h-full flex-col">
      {/* Sticky identity header */}
      <div className="border-b border-border px-4 py-3">
        {isLoading && (
          <div className="space-y-2">
            <Skeleton className="h-5 w-40" />
            <Skeleton className="h-3.5 w-56" />
          </div>
        )}
        {isError && <ErrorState size="inline" error={error} onRetry={() => void refetch()} />}
        {data && (
          <>
            <div className="flex items-center gap-2">
              <KindBadge kind={data.symbol.kind} />
              <span className="truncate text-base font-semibold text-foreground">
                {data.symbol.name}
              </span>
            </div>
            <div className="mt-1 truncate font-mono text-xs text-muted-foreground">
              {data.symbol.qualified_name}
            </div>
            <div className="truncate font-mono text-xs text-muted-foreground">
              {data.symbol.file_path}:{data.symbol.start_line}
            </div>
          </>
        )}
      </div>

      <Tabs
        value={tab}
        onValueChange={(value) => setTab(value as TabKey)}
        className="min-h-0 flex-1 gap-0"
      >
        <TabsList variant="scrollable" className="shrink-0 rounded-none">
          {TABS.map((t) => (
            <TabsTrigger key={t.key} value={t.key} className="text-xs">
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <ScrollArea className="min-h-0 flex-1">
          {TABS.map((t) => (
            <TabsContent key={t.key} value={t.key}>
              {!data ? (
                isError ? null : (
                  <div className="p-4">
                    <Loading />
                  </div>
                )
              ) : t.key === "relationships" ? (
                <RelationshipsTab relations={data.relations} />
              ) : t.key === "rationale" ? (
                <RationaleTab qualifiedName={selectedQn} />
              ) : t.key === "evidence" ? (
                <EvidenceTab qualifiedName={selectedQn} />
              ) : (
                <HistoryTab path={data.symbol.file_path} />
              )}
            </TabsContent>
          ))}
        </ScrollArea>
      </Tabs>
    </div>
  );
}
