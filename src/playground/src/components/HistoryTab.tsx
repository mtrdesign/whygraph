import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { Spinner, EmptyState } from "../lib/ui";
import { EvidenceList } from "./EvidenceList";

// The History tab — area history for the symbol's file (path-keyed), reaching
// commits that line-blame cannot (deleted/renamed/rewritten code).
export function HistoryTab({ path }: { path: string }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["history", path],
    queryFn: () => api.history(path),
  });

  if (isLoading) return <div className="p-3"><Spinner label="Loading history…" /></div>;
  if (isError)
    return <EmptyState>Failed to load history: {(error as Error).message}</EmptyState>;
  return (
    <EvidenceList
      items={data?.evidence ?? []}
      empty="No commits recorded for this file's history."
    />
  );
}
