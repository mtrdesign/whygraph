import { useProjectQuery } from "../lib/project";
import { Loading } from "./Loading";
import { EvidenceList } from "./EvidenceList";
import { ErrorState } from "./state/ErrorState";

// The Evidence tab - always available and LLM-free (line-blame + linked PRs/issues).
export function EvidenceTab({ qualifiedName }: { qualifiedName: string }) {
  const { data, isLoading, isError, error, refetch } = useProjectQuery(["evidence", qualifiedName], (api) =>
    api.evidence(qualifiedName),
  );

  if (isLoading) return <div className="p-3"><Loading label="Loading evidence…" /></div>;
  if (isError)
    return (
      <ErrorState
        className="p-3"
        title="Couldn't load evidence"
        error={error}
        onRetry={() => void refetch()}
      />
    );
  return (
    <EvidenceList
      items={data?.evidence ?? []}
      empty="No commits touch this symbol in the scanned history. Rescan after new commits."
    />
  );
}
