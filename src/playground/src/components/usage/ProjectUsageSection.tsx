import { useId, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { budgetsApi, projectKey, projectUsageApi, type Budget, type ProjectSummary } from "../../api";
import { errorMessage } from "../../lib/apiErrors";
import { formatPct, formatTokens, formatUsd } from "../../lib/format";
import { canAdmin, usePortalState, useRole } from "../../lib/identity";
import { formatResetsAt } from "../../lib/usageRange";
import { Field } from "../portal/Field";
import { ConfirmDialog } from "../portal/ConfirmDialog";
import { SectionForm } from "../settings/SectionForm";
import { SettingsSection } from "../settings/SettingsLayout";
import { ErrorState } from "../state/ErrorState";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Skeleton } from "../ui/skeleton";
import { Switch } from "../ui/switch";
import { BreakdownTable } from "./BreakdownTable";
import { parseAmount, useBudgetWritten } from "./BudgetsTab";
import { EstimatedNote, SpendBar, SplitLine, StatTile, UnpricedNote } from "./parts";
import { DailyCostChart } from "./UsageChart";

/** The project payload's budget block as the editor's `Budget` (it has the spend and the percentage beside it). */
function projectBudget(project: ProjectSummary): Budget | null {
  const u = project.usage;
  if (!u?.budget) return null;
  return { ...u.budget, spent_usd: u.month_spend_usd, pct: u.pct };
}

/**
 * The project's monthly budget as a section form (R3): the amount and the hard
 * stop are staged and saved by **Save**; **Remove budget** acts at once after a
 * confirmation. Server refusals (`budget_above_org`, ...) are worded by the
 * error registry.
 */
function ProjectBudgetForm({ slug, budget }: { slug: string; budget: Budget | null }) {
  const id = useId();
  const written = useBudgetWritten();
  const target = { scope: "project" as const, slug };
  const savedAmount = budget ? String(budget.monthly_usd) : "";
  const savedStop = budget?.hard_stop ?? false;
  const [amount, setAmount] = useState(savedAmount);
  const [hardStop, setHardStop] = useState(savedStop);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const remove = useMutation({
    mutationFn: () => budgetsApi.remove(target),
    onSuccess: async () => {
      setConfirmRemove(false);
      setAmount("");
      setHardStop(false);
      await written(target);
    },
  });
  const dirty = amount.trim() !== savedAmount || hardStop !== savedStop;

  return (
    <div className="flex flex-col gap-2 border-t border-border pt-4" data-testid="project-budget">
      <div className="row-wrap gap-y-0.5">
        <h3 className="text-[13px] font-medium">Project budget</h3>
        <span className="text-xs text-muted-foreground">
          Caps what this project's LLM calls may cost in a month, on top of the organization's budget.
        </span>
      </div>
      <SectionForm
        name="Project budget"
        dirty={dirty}
        saveDisabled={amount.trim() === ""}
        onSave={async () => {
          const parsed = parseAmount(amount);
          if ("error" in parsed) {
            setFieldError(parsed.error);
            return false;
          }
          await budgetsApi.put(target, { monthly_usd: parsed.amount, hard_stop: hardStop });
          await written(target);
          setAmount(String(parsed.amount));
          return true;
        }}
        onDiscard={() => {
          setAmount(savedAmount);
          setHardStop(savedStop);
          setFieldError(null);
        }}
      >
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Monthly budget (USD)" className="w-44" error={fieldError ?? undefined}>
            {(p) => (
              <Input
                {...p}
                inputMode="decimal"
                placeholder="100"
                value={amount}
                onChange={(e) => {
                  setAmount(e.target.value);
                  setFieldError(null);
                }}
              />
            )}
          </Field>
          <div className="flex h-8 items-center gap-2">
            <Switch id={`${id}-hard`} checked={hardStop} onCheckedChange={(v) => setHardStop(!!v)} />
            <Label htmlFor={`${id}-hard`} className="font-normal">
              Hard stop
            </Label>
          </div>
          {budget && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="ml-auto"
              onClick={() => {
                remove.reset();
                setConfirmRemove(true);
              }}
            >
              Remove budget
            </Button>
          )}
        </div>
      </SectionForm>
      <ConfirmDialog
        open={confirmRemove}
        onOpenChange={setConfirmRemove}
        title="Remove the project budget?"
        description="This project's LLM calls are then capped only by the organization's budget."
        confirmLabel="Remove budget"
        pending={remove.isPending}
        error={remove.isError ? errorMessage(remove.error) : null}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}

/**
 * The project settings' **Usage** section (plan section 4.13, id `budgets`), for
 * callers with `project.usage`: this month's totals and daily series, the people
 * who spent it (production), and, for org admins and owners, the project's budget.
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
    <SettingsSection
      id="budgets"
      title="Usage"
      testId="project-usage"
      description={`Estimated LLM spend on this project this month.${resets ? ` Resets ${formatResetsAt(resets)}.` : ""}`}
    >
      {report.isLoading && <Skeleton className="h-40" />}
      {report.isError && (
        <ErrorState error={report.error} title="Couldn't load this project's usage" onRetry={() => void report.refetch()} />
      )}
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
      {/* The budget form waits for the report so it never stays live beside a failed load (ER-4). */}
      {editBudget && !report.isError && (
        <ProjectBudgetForm slug={slug} budget={budget} />
      )}
    </SettingsSection>
  );
}
