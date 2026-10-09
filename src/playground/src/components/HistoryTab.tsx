import { useProjectQuery } from "../lib/project";
import { Loading } from "./Loading";
import { EvidenceList } from "./EvidenceList";
import { ErrorState } from "./state/ErrorState";

// The History tab - area history for the symbol's file (path-keyed), reaching
// commits that line-blame cannot (deleted/renamed/rewritten code).
export function HistoryTab({ path }: { path: string }) {
  const { data, isLoading, isError, error, refetch } = useProjectQuery(["history", path], (api) =>
    api.history(path),
  );

  if (isLoading) return <div className="p-3"><Loading label="Loading history…" /></div>;
  if (isError)
    return (
      <ErrorState
        className="p-3"
        title="Couldn't load history"
        error={error}
        onRetry={() => void refetch()}
      />
    );
  return (
    <EvidenceList
      items={data?.evidence ?? []}
      empty="No commits recorded for this file in the scanned history. Rescan after new commits."
    />
  );
}
