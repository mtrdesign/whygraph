import { budgetNoticeText } from "../../lib/budgetBanner";
import { cn } from "@/lib/utils";

/**
 * A hard-stopped budget's notice (plan section 4.13): on the project pages, in place of
 * the Chat composer, inline for a persisted budget-stop row and beside a disabled Generate.
 * `message` overrides the scope's default wording (the server's own line for a live stop).
 */
export function BudgetNotice({
  scope,
  message,
  testId = "budget-notice",
  className,
}: {
  scope?: string | null;
  message?: string;
  testId?: string;
  className?: string;
}) {
  return (
    <div
      role="status"
      data-testid={testId}
      data-scope={scope ?? undefined}
      className={cn(
        "rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive",
        className,
      )}
    >
      {message ?? budgetNoticeText(scope)}
    </div>
  );
}
