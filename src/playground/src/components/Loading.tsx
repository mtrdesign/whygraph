import { Spinner } from "./ui/spinner";

/** A spinner with an optional label - the in-flight state of a query. */
export function Loading({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <Spinner />
      {label}
    </div>
  );
}
