import { useReadOnly } from "../../lib/identity";

/** Shown over an org the signed-in instance admin is not a member of. */
export function ReaderBanner() {
  if (!useReadOnly()) return null;
  return (
    <div
      role="status"
      data-testid="reader-banner"
      className="shrink-0 border-b border-border bg-muted px-4 py-1.5 text-center text-[13px] text-muted-foreground"
    >
      Viewing as instance admin (read-only)
    </div>
  );
}
