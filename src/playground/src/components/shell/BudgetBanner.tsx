import { useState, type ReactNode } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import { XIcon } from "lucide-react";
import type { StateUsage, UsageGauge } from "../../api";
import {
  MEMBER_STOPPED_LINE,
  budgetNoticeText,
  dismiss,
  isDismissed,
  thresholdOf,
  type Threshold,
} from "../../lib/budgetBanner";
import { formatPct, formatUsd } from "../../lib/format";
import { usePortalState } from "../../lib/identity";
import { formatResetsOn } from "../../lib/usageRange";
import { cn } from "@/lib/utils";
import { alertVariants } from "../ui/alert";
import { Button } from "../ui/button";
import { StatusPill } from "../ui/status-pill";

/** The one-line sentence a soft budget adds at 100%: it warns and does not stop. */
export const SOFT_100_LINE = "Spending continues: this budget has no hard stop.";

/** The words for a viewer who cannot change budgets. */
export const ASK_ADMIN_LINE = "Ask an owner or admin to raise it.";

/**
 * One strip of the banner stack: a tone (`info` at 50%, `warning` above and for a
 * stop), the message, an optional action link and, when `dismissible`, a button
 * that hides it. Hard-stop strips are never dismissible (R4).
 */
export function BannerStrip({
  tone,
  testId,
  threshold,
  scope,
  action,
  onDismiss,
  children,
}: {
  tone: "info" | "warning";
  testId: string;
  threshold?: Threshold;
  scope?: string;
  action?: ReactNode;
  /** Present when the strip can be dismissed for the month. */
  onDismiss?: () => void;
  children: ReactNode;
}) {
  return (
    <div
      role="status"
      data-testid={testId}
      data-threshold={threshold}
      data-tone={tone}
      data-scope={scope}
      className={cn(
        alertVariants({ variant: tone }),
        "flex shrink-0 items-center gap-2 rounded-none border-x-0 border-t-0 px-4 py-1.5 text-[13px]",
      )}
    >
      <div className="row-wrap min-w-0 flex-1 justify-center gap-x-2 gap-y-1 text-center">
        {children}
        {action}
      </div>
      {onDismiss && (
        <Button variant="ghost" size="icon-sm" aria-label="Dismiss for this month" onClick={onDismiss}>
          <XIcon />
        </Button>
      )}
    </div>
  );
}

const STOPPED_PILL = <StatusPill tone="warn" label="Stopped" title="Monthly budget reached" />;

function Bar({
  scope,
  month,
  threshold,
  stopped = false,
  action,
  children,
}: {
  scope: string;
  month: string;
  threshold: Threshold;
  stopped?: boolean;
  action: ReactNode;
  children: ReactNode;
}) {
  const [hidden, setHidden] = useState(false);
  // Only a hard stop stays up: 50, 75 and a soft budget's 100% hide for the month (R4).
  const dismissible = !stopped;
  if (dismissible && (hidden || isDismissed(scope, month, threshold))) return null;
  return (
    <BannerStrip
      tone={threshold === 50 ? "info" : "warning"}
      testId={`budget-banner-${scope}`}
      threshold={threshold}
      action={action}
      onDismiss={
        dismissible
          ? () => {
              dismiss(scope, month, threshold);
              setHidden(true);
            }
          : undefined
      }
    >
      {stopped && STOPPED_PILL}
      {children}
    </BannerStrip>
  );
}

const linkClass = "font-medium underline underline-offset-2 hover:text-foreground";

function MemberBanner({
  me,
  usage,
  orgUsage,
  covered,
}: {
  me: UsageGauge;
  usage: StateUsage;
  /** The caller can review the organization's budgets (`ORG_USAGE`). */
  orgUsage: boolean;
  /** A project page already says the stop in its own banner (one reason per place). */
  covered: boolean;
}) {
  const threshold = thresholdOf(me.pct);
  if (threshold === null || me.budget_usd === null) return null;
  const stopped = threshold === 100 && (me.blocked || me.hard_stop);
  if (stopped && covered) return null;
  const amount = `${formatUsd(me.spent_usd)} of ${formatUsd(me.budget_usd)}`;
  const resets = `Resets ${formatResetsOn(usage.resets_at)}.`;
  return (
    <Bar
      scope="me"
      month={usage.month}
      threshold={threshold}
      stopped={stopped}
      action={
        <Link to="/usage/me" className={linkClass}>
          My usage
        </Link>
      }
    >
      {stopped ? (
        <span>
          {MEMBER_STOPPED_LINE} {amount} spent. {resets}
          {!orgUsage && ` ${ASK_ADMIN_LINE}`}
        </span>
      ) : threshold === 100 ? (
        <span>
          You've reached your monthly budget ({amount}). {SOFT_100_LINE} {resets}
        </span>
      ) : (
        <span>
          You've used {formatPct(me.pct)} of your monthly budget ({amount}). {resets}
        </span>
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
  const stopped = orgThreshold === 100 && (org.blocked || org.hard_stop);
  const soft100 = orgThreshold === 100 && !stopped;
  return (
    <Bar
      scope="org"
      month={usage.month}
      threshold={top}
      stopped={stopped}
      action={
        <Link to="/usage" search={{ tab: "budgets" }} className={linkClass}>
          Review budgets
        </Link>
      }
    >
      {orgThreshold !== null && org.budget_usd !== null && (
        <span data-testid="budget-banner-org-line">
          {orgName} is at {formatPct(org.pct)} of its monthly budget ({formatUsd(org.spent_usd)} of{" "}
          {formatUsd(org.budget_usd)}).
          {soft100 && ` ${SOFT_100_LINE}`}
          {stopped && ` Resets ${formatResetsOn(usage.resets_at)}.`}
        </span>
      )}
      {projects.length > 0 && (
        <span data-testid="budget-banner-projects-line">
          Projects at or over 50%: {projects.map((p) => `${p.name} (${formatPct(p.pct)})`).join(", ")}.
        </span>
      )}
    </Bar>
  );
}

/**
 * A hard-stopped project's notice (USE-2): what is paused, what still works, when it
 * resets and who can change it. Not dismissible; Chat says the same in place of its
 * composer, so it shows no banner there (USE-3).
 */
export function ProjectStopBanner({ scope }: { scope?: string | null }) {
  const state = usePortalState().data;
  const usage = state?.usage;
  return (
    <BannerStrip
      tone="warning"
      testId="budget-notice"
      scope={scope ?? undefined}
      action={
        usage?.org ? (
          <Link to="/usage" search={{ tab: "budgets" }} className={linkClass}>
            Review budgets
          </Link>
        ) : undefined
      }
    >
      {STOPPED_PILL}
      <span>
        {budgetNoticeText(scope, state?.mode)}
        {usage?.resets_at && ` Resets ${formatResetsOn(usage.resets_at)}.`}
        {!usage?.org && ` ${ASK_ADMIN_LINE}`}
      </span>
    </BannerStrip>
  );
}

/**
 * The budget notices (plan section 4.14), computed from `state.usage`. A member's own
 * budget shows on every org page at 50, 75 and 100%; owners and org admins also get one
 * banner for the org and its projects, on the Projects and Usage & cost pages. Every
 * level can be dismissed for the month except a hard stop (R4). `projectStopped` is a
 * project page that already shows its own stop banner.
 */
export function BudgetBanner({ projectStopped = false }: { projectStopped?: boolean }) {
  const state = usePortalState().data;
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const usage = state?.usage;
  if (!usage) return null;
  const orgPage = pathname === "/" || pathname.startsWith("/usage");
  return (
    <>
      {usage.me && <MemberBanner me={usage.me} usage={usage} orgUsage={!!usage.org} covered={projectStopped} />}
      {usage.org && orgPage && <OrgBanner usage={usage} orgName={state?.org?.name ?? "The organization"} />}
    </>
  );
}
