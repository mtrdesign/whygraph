import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { Spinner, EmptyState } from "../lib/ui";
import { EvidenceList } from "./EvidenceList";

// The Evidence tab — always available and LLM-free (line-blame + linked PRs/issues).
export function EvidenceTab({ qualifiedName }: { qualifiedName: string }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["evidence", qualifiedName],
    queryFn: () => api.evidence(qualifiedName),
  });

  if (isLoading) return <div className="p-3"><Spinner label="Loading evidence…" /></div>;
  if (isError)
    return <EmptyState>Failed to load evidence: {(error as Error).message}</EmptyState>;
  return (
    <EvidenceList items={data?.evidence ?? []} empty="No historical evidence for this symbol." />
  );
}
