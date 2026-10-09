import { Skeleton } from "../ui/skeleton";

// Loading placeholders in the shape of what lands, so nothing shifts when the data
// arrives (M2f-3 ER-5). Each carries `aria-busy` and a screen-reader label.

function Busy({ label = "Loading", className, children }: { label?: string; className?: string; children: React.ReactNode }) {
  return (
    <div className={className} aria-busy="true" role="status" data-testid="skeleton">
      <span className="sr-only">{label}</span>
      {children}
    </div>
  );
}

/** A page: a title line, a sub line and two blocks. */
export function PageSkeleton({ label }: { label?: string }) {
  return (
    <Busy label={label} className="mx-auto flex w-full max-w-5xl flex-col gap-4 p-6">
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-4 w-72 max-w-full" />
      <Skeleton className="h-32 w-full" />
      <Skeleton className="h-48 w-full" />
    </Busy>
  );
}

/** A grid of cards (the projects list, tiles). */
export function CardGridSkeleton({ count = 6, label }: { count?: number; label?: string }) {
  return (
    <Busy label={label} className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} className="h-28 w-full rounded-xl" />
      ))}
    </Busy>
  );
}

/** A table: a header row and `rows` rows of `cols` cells. */
export function TableSkeleton({ rows = 5, cols = 4, label }: { rows?: number; cols?: number; label?: string }) {
  return (
    <Busy label={label} className="flex flex-col gap-2">
      {Array.from({ length: rows + 1 }, (_, r) => (
        <div key={r} className="flex gap-3">
          {Array.from({ length: cols }, (_, c) => (
            <Skeleton key={c} className={r === 0 ? "h-3 flex-1" : "h-5 flex-1"} />
          ))}
        </div>
      ))}
    </Busy>
  );
}

/** A form: `fields` label + input pairs and a button. */
export function FormSkeleton({ fields = 3, label }: { fields?: number; label?: string }) {
  return (
    <Busy label={label} className="flex flex-col gap-4">
      {Array.from({ length: fields }, (_, i) => (
        <div key={i} className="flex flex-col gap-1.5">
          <Skeleton className="h-3.5 w-28" />
          <Skeleton className="h-8 w-full" />
        </div>
      ))}
      <Skeleton className="h-8 w-24" />
    </Busy>
  );
}

/** A detail view: a heading and a few lines of text. */
export function DetailSkeleton({ label }: { label?: string }) {
  return (
    <Busy label={label} className="flex flex-col gap-3">
      <Skeleton className="h-5 w-56 max-w-full" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-5/6" />
      <Skeleton className="h-4 w-2/3" />
    </Busy>
  );
}
