import { useProjectQuery } from "../lib/project";
import { Empty, EmptyDescription } from "./ui/empty";
import { Loading } from "./Loading";
import { EvidenceList } from "./EvidenceList";

// The Evidence tab — always available and LLM-free (line-blame + linked PRs/issues).
export function EvidenceTab({ qualifiedName }: { qualifiedName: string }) {
  const { data, isLoading, isError, error } = useProjectQuery(["evidence", qualifiedName], (api) =>
    api.evidence(qualifiedName),
  );

  if (isLoading) return <div className="p-3"><Loading label="Loading evidence…" /></div>;
  if (isError)
    return (
      <Empty className="p-4">
        <EmptyDescription>Failed to load evidence: {(error as Error).message}</EmptyDescription>
      </Empty>
    );
  return (
    <EvidenceList items={data?.evidence ?? []} empty="No historical evidence for this symbol." />
  );
}
