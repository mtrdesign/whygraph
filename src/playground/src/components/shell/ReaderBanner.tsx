import { useReadOnly } from "../../lib/identity";
import { alertVariants } from "../ui/alert";
import { cn } from "@/lib/utils";

/** Shown over an org the signed-in instance admin is not a member of. */
export function ReaderBanner() {
  if (!useReadOnly()) return null;
  return (
    <div
      role="status"
      data-testid="reader-banner"
      className={cn(
        alertVariants({ variant: "info" }),
        "shrink-0 rounded-none border-x-0 border-t-0 px-4 py-1.5 text-center text-[13px]",
      )}
    >
      Viewing as instance admin (read-only)
    </div>
  );
}
