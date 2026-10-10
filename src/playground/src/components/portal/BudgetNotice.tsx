import { budgetNoticeText } from "../../lib/budgetBanner";
import { usePortalState } from "../../lib/identity";
import { cn } from "@/lib/utils";
import { Alert } from "../ui/alert";

/**
 * A hard-stopped budget's notice (a state, not an error: the `warning` tone) (plan section 4.13): on the project pages, in place of
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
  const mode = usePortalState().data?.mode;
  return (
    <Alert
      variant="warning"
      role="status"
      data-testid={testId}
      data-scope={scope ?? undefined}
      className={cn("px-3", className)}
    >
      {message ?? budgetNoticeText(scope, mode)}
    </Alert>
  );
}
