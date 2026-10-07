import { useQuery } from "@tanstack/react-query";
import { projectKey, projectUsageApi, type Budget, type ProjectSummary } from "../../api";
import { authMessage } from "../../lib/authErrors";
import { formatPct, formatTokens, formatUsd } from "../../lib/format";
import { canAdmin, usePortalState, useRole } from "../../lib/identity";
import { formatResetsAt } from "../../lib/usageRange";
import { Skeleton } from "../ui/skeleton";
import { BreakdownTable } from "./BreakdownTable";
import { BudgetEditor } from "./BudgetsTab";
import {
  EstimatedNote,
  SpendBar,
  SplitLine,
  StatTile,
  UnpricedNote,
  UsageSection,
} from "./parts";
import { DailyCostChart } from "./UsageChart";

/** The project payload's budget block as the editor's `Budget` (it has the spend and the percentage beside it). */
function projectBudget(project: ProjectSummary): Budget | null {
  const u = project.usage;
  if (!u?.budget) return null;
  return { ...u.budget, spent_usd: u.month_spend_usd, pct: u.pct };
}

/**
 * The project settings' **Usage** section (plan section 4.13), for callers with
 * `project.usage`: this month's totals and daily series, the people who spent it
 * (production), and, for org admins and owners, the project's budget.
 */
export function ProjectUsageSection({ slug, project }: { slug: string; project: ProjectSummary }) {
  const role = useRole();
  const resets = usePortalState().data?.usage?.resets_at;
  const report = useQuery({
    queryKey: projectKey(slug, "usage"),
    queryFn: () => projectUsageApi(slug).report(),
  });
  const budget = projectBudget(project);
  const editBudget = canAdmin(role);
  const data = report.data;

  return (
    <div id="settings-usage" className="scroll-mt-4" data-testid="project-usage">
      <UsageSection
        title="Usage"
        description={`Estimated LLM spend on this project this month.${resets ? ` Resets ${formatResetsAt(resets)}.` : ""}`}
      >
        {report.isLoading && <Skeleton className="h-40" />}
        {report.isError && <p className="text-sm text-destructive">Failed to load: {authMessage(report.error)}</p>}
        {data && (
          <>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
              <StatTile
                testId="project-usage-spend"
                label="This month"
                value={formatUsd(data.totals.cost_usd)}
                detail={`${data.totals.calls.toLocaleString("en-US")} calls`}
              />
              <StatTile
                label="Tokens in / out"
                value={`${formatTokens(data.totals.input_tokens)} / ${formatTokens(data.totals.output_tokens)}`}
              />
              <StatTile
                testId="project-usage-budget"
                label="Project budget"
                value={budget ? formatPct(budget.pct) : "None"}
                detail={
                  budget ? (
                    <span className="flex flex-col gap-1">
                      <span>
                        of {formatUsd(budget.monthly_usd)}
                        {budget.hard_stop ? ", hard stop" : ""}
                      </span>
                      <SpendBar pct={budget.pct} />
                    </span>
                  ) : undefined
                }
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <SplitLine split={data.split} />
              <UnpricedNote calls={data.totals.unpriced_calls} />
              <EstimatedNote />
            </div>
            <DailyCostChart series={data.series} />
            {data.members.length > 0 && (
              <div className="flex flex-col gap-2" data-testid="project-usage-members">
                <h3 className="text-xs font-medium">By person</h3>
                <BreakdownTable group="member" rows={data.members} compact />
              </div>
            )}
          </>
        )}
        {editBudget && (
          <BudgetEditor
            key={`${budget?.monthly_usd ?? "none"}:${budget?.hard_stop ?? ""}`}
            target={{ scope: "project", slug }}
            budget={budget}
            editable
            title="Project budget"
            description="Caps what this project's LLM calls may cost in a month, on top of the organization's budget."
            testId="project-budget"
          />
        )}
      </UsageSection>
    </div>
  );
}
