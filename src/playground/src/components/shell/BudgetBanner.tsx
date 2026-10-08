import { useState } from "react";
import { useRouterState } from "@tanstack/react-router";
import { XIcon } from "lucide-react";
import type { StateUsage, UsageGauge } from "../../api";
import {
  MEMBER_STOPPED_LINE,
  dismiss,
  isDismissed,
  thresholdOf,
  type Threshold,
} from "../../lib/budgetBanner";
import { formatUsd } from "../../lib/format";
import { usePortalState } from "../../lib/identity";
import { formatResetsAt } from "../../lib/usageRange";
import { cn } from "@/lib/utils";
import { Button } from "../ui/button";

function Bar({
  scope,
  month,
  threshold,
  children,
}: {
  scope: string;
  month: string;
  threshold: Threshold;
  children: React.ReactNode;
}) {
  const [hidden, setHidden] = useState(false);
  if (hidden || (threshold < 100 && isDismissed(scope, month, threshold))) return null;
  return (
    <div
      role="status"
      data-testid={`budget-banner-${scope}`}
      data-threshold={threshold}
      className={cn(
        "flex shrink-0 items-center gap-2 border-b px-4 py-1.5 text-[13px]",
        threshold >= 100
          ? "border-destructive/30 bg-destructive/10 text-destructive"
          : "border-warning/30 bg-warning/10 text-warning",
      )}
    >
      <div className="min-w-0 flex-1 text-center">{children}</div>
      {threshold < 100 && (
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Dismiss for this month"
          onClick={() => {
            dismiss(scope, month, threshold);
            setHidden(true);
          }}
        >
          <XIcon />
        </Button>
      )}
    </div>
  );
}

function MemberBanner({ me, usage }: { me: UsageGauge; usage: StateUsage }) {
  const threshold = thresholdOf(me.pct);
  if (threshold === null || me.budget_usd === null) return null;
  const stopped = threshold === 100 && (me.blocked || me.hard_stop);
  return (
    <Bar scope="me" month={usage.month} threshold={threshold}>
      {stopped ? (
        MEMBER_STOPPED_LINE
      ) : threshold === 100 ? (
        <>
          You've reached your monthly budget ({formatUsd(me.spent_usd)} of {formatUsd(me.budget_usd)}). Resets{" "}
          {formatResetsAt(usage.resets_at)}.
        </>
      ) : (
        <>
          You've used {threshold}% of your monthly budget ({formatUsd(me.spent_usd)} of{" "}
          {formatUsd(me.budget_usd)}). Resets {formatResetsAt(usage.resets_at)}.
        </>
      )}
    </Bar>
  );
}

function OrgBanner({ usage, orgName }: { usage: StateUsage; orgName: string }) {
  const org = usage.org;
  const orgThreshold = thresholdOf(org?.pct);
  const projects = (usage.projects_over ?? [])
    .map((p) => ({ ...p, threshold: thresholdOf(p.pct) }))
    .filter((p): p is typeof p & { threshold: Threshold } => p.threshold !== null);
  // The notice's level is the highest across the org and its projects; a new, higher
  // level shows again even after a lower one was dismissed.
  const top = Math.max(orgThreshold ?? 0, ...projects.map((p) => p.threshold)) as 0 | Threshold;
  if (top === 0 || !org) return null;
  return (
    <Bar scope="org" month={usage.month} threshold={top}>
      {orgThreshold !== null && org.budget_usd !== null && (
        <span data-testid="budget-banner-org-line">
          {orgName} is at {orgThreshold}% of its monthly budget ({formatUsd(org.spent_usd)} of{" "}
          {formatUsd(org.budget_usd)}).{" "}
        </span>
      )}
      {projects.length > 0 && (
        <span data-testid="budget-banner-projects-line">
          Projects at or over 50%: {projects.map((p) => `${p.name} (${p.threshold}%)`).join(", ")}.
        </span>
      )}
    </Bar>
  );
}

/**
 * The budget notices (plan section 4.8), computed from `state.usage`. A member's own
 * budget shows on every org page at 50, 75 and 100%; owners and org admins also get one
 * banner for the org and its projects, on the Projects and Usage & cost pages. 50 and 75
 * can be dismissed for the month; 100 cannot.
 */
export function BudgetBanner() {
  const state = usePortalState().data;
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const usage = state?.usage;
  if (!usage) return null;
  const orgPage = pathname === "/" || pathname.startsWith("/usage");
  return (
    <>
      {usage.me && <MemberBanner me={usage.me} usage={usage} />}
      {usage.org && orgPage && <OrgBanner usage={usage} orgName={state?.org?.name ?? "The organization"} />}
    </>
  );
}
