import { usePortalState } from "../../lib/identity";
import { alertVariants } from "../ui/alert";
import { cn } from "@/lib/utils";

/** Shown while a platform administrator has suspended the org (plan 59, §4.5); the reason is text. */
export function SuspendedBanner() {
  const suspended = usePortalState().data?.org?.suspended;
  if (!suspended) return null;
  return (
    <div
      role="status"
      data-testid="suspended-banner"
      className={cn(
        alertVariants({ variant: "warning" }),
        "shrink-0 rounded-none border-x-0 border-t-0 px-4 py-1.5 text-center text-[13px]",
      )}
    >
      This organization is suspended: {suspended.reason}. Data is read-only.
    </div>
  );
}
