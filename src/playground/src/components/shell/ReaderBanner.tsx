import { usePortalState, useReadOnly } from "../../lib/identity";
import { formatClock } from "../../lib/format";
import { alertVariants } from "../ui/alert";
import { cn } from "@/lib/utils";

/** Shown over an org the signed-in platform administrator is not a member of, until the grant ends (D59). */
export function ReaderBanner() {
  const state = usePortalState().data;
  if (!useReadOnly()) return null;
  const until = state?.org?.reader_until;
  return (
    <div
      role="status"
      data-testid="reader-banner"
      className={cn(
        alertVariants({ variant: "info" }),
        "shrink-0 rounded-none border-x-0 border-t-0 px-4 py-1.5 text-center text-[13px]",
      )}
    >
      {until
        ? `Viewing as a platform administrator, read-only until ${formatClock(until)}.`
        : "Viewing as a platform administrator, read-only."}
    </div>
  );
}
