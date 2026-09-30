import { useProjectQuery } from "../lib/project";
import { Empty, EmptyDescription } from "./ui/empty";
import { Loading } from "./Loading";
import { EvidenceList } from "./EvidenceList";

// The History tab — area history for the symbol's file (path-keyed), reaching
// commits that line-blame cannot (deleted/renamed/rewritten code).
export function HistoryTab({ path }: { path: string }) {
  const { data, isLoading, isError, error } = useProjectQuery(["history", path], (api) =>
    api.history(path),
  );

  if (isLoading) return <div className="p-3"><Loading label="Loading history…" /></div>;
  if (isError)
    return (
      <Empty className="p-4">
        <EmptyDescription>Failed to load history: {(error as Error).message}</EmptyDescription>
      </Empty>
    );
  return (
    <EvidenceList
      items={data?.evidence ?? []}
      empty="No commits recorded for this file's history."
    />
  );
}
