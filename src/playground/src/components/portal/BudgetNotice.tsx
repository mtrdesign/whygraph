import { Link } from "@tanstack/react-router";
import { budgetNoticeText } from "../../lib/budgetBanner";
import { usePortalState } from "../../lib/identity";
import { formatResetsOn } from "../../lib/usageRange";
import { cn } from "@/lib/utils";
import { Alert } from "../ui/alert";
import { ASK_ADMIN_LINE, budgetsLinkText } from "../shell/BudgetBanner";

/**
 * A hard-stopped budget's notice (a state, not an error: the `warning` tone) (plan section 4.14): in place of
 * the Chat composer, inline for a persisted budget-stop row and beside a disabled Generate.
 * `message` overrides the scope's default wording (the server's own line for a live stop).
 *
 * With `details` (the default when no `message` is given) it also says when the budget resets
 * and what to do (USE-2): **Review budgets** for whoever can see the budgets (`ORG_USAGE`),
 * **Raise it in Budgets** for local mode's user, otherwise "Ask an owner or admin to raise it".
 */
export function BudgetNotice({
  scope,
  message,
  details,
  testId = "budget-notice",
  className,
}: {
  scope?: string | null;
  message?: string;
  details?: boolean;
  testId?: string;
  className?: string;
}) {
  const state = usePortalState().data;
  const usage = state?.usage;
  const showDetails = details ?? message === undefined;
  return (
    <Alert
      variant="warning"
      role="status"
      data-testid={testId}
      data-scope={scope ?? undefined}
      className={cn("px-3", className)}
    >
      <span>
        {message ?? budgetNoticeText(scope, state?.mode)}
        {showDetails && usage?.resets_at && (
          <span data-testid={`${testId}-resets`}> Resets {formatResetsOn(usage.resets_at)}.</span>
        )}
        {showDetails &&
          (usage?.org ? (
            <>
              {" "}
              <Link
                to="/usage"
                search={{ tab: "budgets" }}
                className="font-medium underline underline-offset-2 hover:text-foreground"
                data-testid={`${testId}-action`}
              >
                {budgetsLinkText(state?.mode)}
              </Link>
            </>
          ) : (
            <span data-testid={`${testId}-action`}> {ASK_ADMIN_LINE}</span>
          ))}
      </span>
    </Alert>
  );
}
