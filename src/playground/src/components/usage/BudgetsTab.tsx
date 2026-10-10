import { DisabledReason } from "../state/DisabledReason";
import { useId, useState, type FormEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  budgetsApi,
  membersApi,
  portalApi,
  portalKey,
  projectKey,
  type Budget,
  type BudgetTarget,
  type BudgetsView,
  type Member,
} from "../../api";
import { authMessage } from "../../lib/authErrors";
import { formatDateTime, formatPct, formatUsd } from "../../lib/format";
import { inheritedLayerLabel } from "../../lib/labels";
import { formatMonth, formatResetsAt } from "../../lib/usageRange";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Skeleton } from "../ui/skeleton";
import { Switch } from "../ui/switch";
import { Field, nativeSelectClass } from "../portal/Field";
import { ResponsiveTable, type Column } from "../layout/ResponsiveTable";
import { SpendBar, UnpricedNote, UsageError, UsageSection } from "./parts";

export const BUDGETS_KEY = portalKey("budgets");

/** The largest monthly budget the server accepts. */
const MAX_MONTHLY_USD = 1_000_000;

export const AMOUNT_HINT = "Enter a monthly budget greater than $0 and at most $1,000,000.";

/** A typed amount as a number, or the message that says why it is not one. */
export function parseAmount(raw: string): { amount: number } | { error: string } {
  const text = raw.trim().replace(/^\$/, "").replace(/,/g, "");
  if (!/^\d+(\.\d+)?$/.test(text)) return { error: AMOUNT_HINT };
  const amount = Number(text);
  if (!Number.isFinite(amount) || amount <= 0 || amount > MAX_MONTHLY_USD) return { error: AMOUNT_HINT };
  return { amount: Math.round(amount * 100) / 100 };
}

/** After a budget write: the budgets, the banners (`state.usage`) and every `llm_block` may have moved. */
export function useBudgetWritten() {
  const queryClient = useQueryClient();
  return async (target: BudgetTarget) => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: BUDGETS_KEY }),
      queryClient.invalidateQueries({ queryKey: portalKey("state") }),
      queryClient.invalidateQueries({ queryKey: portalKey("projects") }),
      target.scope === "project"
        ? queryClient.invalidateQueries({ queryKey: projectKey(target.slug, "project") })
        : Promise.resolve(),
    ]);
  };
}

function spentLine(budget: Budget): string {
  if (budget.spent_usd === null) return `${formatUsd(budget.monthly_usd)} a month for each member`;
  return `${formatUsd(budget.spent_usd)} of ${formatUsd(budget.monthly_usd)} (${formatPct(budget.pct)})`;
}

/**
 * One budget: what was spent against it this month, a bar, and (when `editable`)
 * the amount and hard-stop switch with Save and Remove. Errors from the server
 * (`budget_above_org`, `budget_below_children`, `invalid_amount`, ...) show inline.
 * Reusable for any target: the org, the member default, a member, a project.
 */
export function BudgetEditor({
  target,
  budget,
  title,
  description,
  editable,
  readOnlyReason,
  testId,
}: {
  target: BudgetTarget;
  budget: Budget | null;
  title: ReactNode;
  description?: ReactNode;
  editable: boolean;
  /** Why a viewer who edits other budgets cannot edit this one. */
  readOnlyReason?: string;
  testId?: string;
}) {
  const id = useId();
  const written = useBudgetWritten();
  const [amount, setAmount] = useState(budget ? String(budget.monthly_usd) : "");
  const [hardStop, setHardStop] = useState(budget?.hard_stop ?? false);
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (monthly_usd: number) => budgetsApi.put(target, { monthly_usd, hard_stop: hardStop }),
    onSuccess: async () => {
      setError(null);
      await written(target);
    },
    onError: (err) => setError(authMessage(err)),
  });
  const remove = useMutation({
    mutationFn: () => budgetsApi.remove(target),
    onSuccess: async () => {
      setError(null);
      setAmount("");
      setHardStop(false);
      await written(target);
    },
    onError: (err) => setError(authMessage(err)),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const parsed = parseAmount(amount);
    if ("error" in parsed) {
      setError(parsed.error);
      return;
    }
    save.mutate(parsed.amount);
  };
  const dirty = !budget || Number(amount) !== budget.monthly_usd || hardStop !== budget.hard_stop;
  const busy = save.isPending || remove.isPending;

  return (
    <div className="flex flex-col gap-2 rounded-lg bg-muted/40 p-3" data-testid={testId}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="font-medium">{title}</span>
        {description && <span className="text-xs text-muted-foreground">{description}</span>}
        <span className="ml-auto text-xs tabular-nums text-muted-foreground" data-testid={testId && `${testId}-spent`}>
          {budget ? spentLine(budget) : "No budget"}
        </span>
      </div>
      {budget && <SpendBar pct={budget.pct} />}
      {editable ? (
        <form onSubmit={submit} className="flex flex-wrap items-end gap-3" noValidate>
          <Field label="Monthly budget (USD)" className="w-44" error={error ?? undefined}>
            {(p) => (
              <Input
                {...p}
                inputMode="decimal"
                placeholder="e.g. 100"
                value={amount}
                onChange={(e) => {
                  setAmount(e.target.value);
                  setError(null);
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
          <Button type="submit" size="sm" disabled={busy || !dirty || amount.trim() === ""}>
            {budget ? "Save" : "Set budget"}
          </Button>
          {budget && (
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => remove.mutate()}>
              Remove
            </Button>
          )}
        </form>
      ) : (
        budget && (
          <p className="text-xs text-muted-foreground">
            Hard stop {budget.hard_stop ? "on" : "off"}
            {readOnlyReason ? `. ${readOnlyReason}` : ""}
          </p>
        )
      )}
    </div>
  );
}

/** Add a budget for one of `options` (a member override or a project budget). */
function AddBudget({
  label,
  options,
  toTarget,
  testId,
}: {
  label: string;
  options: { value: string; label: string }[];
  toTarget: (value: string) => BudgetTarget;
  testId: string;
}) {
  const id = useId();
  const written = useBudgetWritten();
  const [choice, setChoice] = useState("");
  const [amount, setAmount] = useState("");
  const [hardStop, setHardStop] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const add = useMutation({
    mutationFn: ({ target, monthly_usd }: { target: BudgetTarget; monthly_usd: number }) =>
      budgetsApi.put(target, { monthly_usd, hard_stop: hardStop }),
    onSuccess: async (_, { target }) => {
      setChoice("");
      setAmount("");
      setHardStop(false);
      setError(null);
      await written(target);
    },
    onError: (err) => setError(authMessage(err)),
  });
  if (options.length === 0) return null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!choice) return;
    const parsed = parseAmount(amount);
    if ("error" in parsed) {
      setError(parsed.error);
      return;
    }
    add.mutate({ target: toTarget(choice), monthly_usd: parsed.amount });
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3" data-testid={testId} noValidate>
      <Field label={label} className="w-56">
        {(p) => (
          <select {...p} className={nativeSelectClass} value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value="">Choose…</option>
            {options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field label="Monthly budget (USD)" className="w-44" error={error ?? undefined}>
        {(p) => (
          <Input
            {...p}
            inputMode="decimal"
            placeholder="e.g. 50"
            value={amount}
            onChange={(e) => {
              setAmount(e.target.value);
              setError(null);
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
      {add.isPending || (choice && amount.trim() !== "") ? (
        <Button type="submit" size="sm" disabled={add.isPending}>
          Add budget
        </Button>
      ) : (
        <DisabledReason reason="Choose who the budget is for and enter an amount.">
          <Button type="submit" size="sm" disabled>
            Add budget
          </Button>
        </DisabledReason>
      )}
    </form>
  );
}

type Alert = BudgetsView["alerts"][number];

function Alerts({ alerts, orgLabel }: { alerts: BudgetsView["alerts"]; orgLabel: string }) {
  if (alerts.length === 0) {
    return <p className="text-sm text-muted-foreground">No budget has crossed 50% this month.</p>;
  }
  const scope: Record<string, string> = { org: orgLabel, project: "Project", member: "Member" };
  const columns: Column<Alert>[] = [
    {
      key: "when",
      header: "When",
      cell: (a) => <span className="whitespace-nowrap text-muted-foreground">{formatDateTime(a.crossed_at)}</span>,
    },
    {
      key: "budget",
      header: "Budget",
      primary: true,
      cell: (a) => (
        <>
          <span className="font-normal text-muted-foreground">{scope[a.scope] ?? a.scope}</span> {a.label ?? ""}
        </>
      ),
    },
    {
      key: "threshold",
      header: "Threshold",
      align: "right",
      cell: (a) => (
        <span className={a.threshold >= 100 ? "text-destructive" : a.threshold >= 75 ? "text-warning" : undefined}>
          {a.threshold}%
        </span>
      ),
    },
    { key: "spent", header: "Spent then", align: "right", cell: (a) => formatUsd(a.spent_usd) },
  ];
  return (
    <div data-testid="budget-alerts">
      <ResponsiveTable columns={columns} rows={alerts} rowKey={(a) => `${a.crossed_at}:${a.scope}:${a.label ?? ""}:${a.threshold}`} />
    </div>
  );
}

function memberLabel(m: Member): string {
  const name = m.display_name || m.github_login || m.uid;
  return m.github_login ? `${name} (@${m.github_login})` : name;
}

/**
 * The Budgets tab (plan section 4.13): the org budget, the member default and
 * per-member overrides (production), per-project budgets, this month's crossings
 * and the unpriced warning. `canEdit` is `org.budgets` (owners and org admins); an
 * org admin still cannot touch their own override or an owner's (the server's
 * `403 forbidden`), so those rows are read-only for them.
 */
export function BudgetsTab({
  canEdit,
  isOwner,
  production,
  viewerUid,
}: {
  canEdit: boolean;
  isOwner: boolean;
  production: boolean;
  viewerUid?: string;
}) {
  const budgets = useQuery({ queryKey: BUDGETS_KEY, queryFn: budgetsApi.get });
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  const members = useQuery({
    queryKey: portalKey("members"),
    queryFn: membersApi.list,
    enabled: production,
  });

  if (budgets.isLoading) return <Skeleton className="h-40" />;
  if (budgets.isError || !budgets.data) {
    return (
      <UsageError
        error={budgets.error}
        what="the budgets"
        title="Couldn't load the budgets"
        onRetry={() => void budgets.refetch()}
      />
    );
  }
  const data = budgets.data;
  // What the top-level budget is called: the organization in production, the portal locally (MODE-5).
  const layer = inheritedLayerLabel(production ? "production" : "local", true);
  const roleOf = new Map((members.data ?? []).map((m) => [m.uid, m.role]));
  // An org admin may not set or remove their own override or an owner's.
  const memberLock = (uid: string): string | undefined => {
    if (isOwner) return undefined;
    if (uid === viewerUid) return "Only an owner can change your own budget.";
    if (roleOf.get(uid) === "owner") return "Only an owner can change an owner's budget.";
    return undefined;
  };
  const overridden = new Set(data.members.map((m) => m.uid));
  const memberOptions = (members.data ?? [])
    .filter((m) => !overridden.has(m.uid) && !m.disabled && !memberLock(m.uid))
    .map((m) => ({ value: m.uid, label: memberLabel(m) }));
  const budgeted = new Set(data.projects.map((p) => p.slug));
  const projectOptions = (projects.data?.projects ?? [])
    .filter((p) => !budgeted.has(p.slug))
    .map((p) => ({ value: p.slug, label: p.name }));
  // Each editor is keyed by its current values, so a saved change from elsewhere re-seeds the form.
  const k = (b: Budget | null) => (b ? `${b.monthly_usd}:${b.hard_stop}` : "none");

  return (
    <div className="flex flex-col gap-6" data-testid="budgets-tab">
      <p className="text-[13px] text-muted-foreground">
        Budgets for {formatMonth(data.month)}, in estimated USD; they reset on {formatResetsAt(data.resets_at)}. At
        50, 75 and 100% the people concerned see a banner, which they can dismiss for the month. With <strong className="font-medium">hard stop</strong> on, a
        spent budget turns new LLM spend off for everyone it covers until the month resets: they can still read
        everything that is already generated.
        {!canEdit && " Owners and org admins change budgets."}
      </p>
      <UnpricedNote calls={data.unpriced_calls} budgets />

      <UsageSection
        title={layer}
        description={production ? "Caps the whole organization, owners included." : "Caps everything this portal spends."}
      >
        <BudgetEditor
          key={k(data.org)}
          target={{ scope: "org" }}
          budget={data.org}
          title={`${layer} budget`}
          editable={canEdit}
          testId="budget-org"
        />
      </UsageSection>

      {production && (
        <UsageSection
          title="Members"
          description="The default caps each member separately; an override replaces it for one person. Both stay at or below the organization's budget."
          testId="budget-members"
        >
          <BudgetEditor
            key={k(data.member_default)}
            target={{ scope: "member_default" }}
            budget={data.member_default}
            title="Default for every member"
            editable={canEdit}
            testId="budget-member-default"
          />
          {data.members.map((m) => {
            const lock = memberLock(m.uid);
            return (
              <BudgetEditor
                key={`${m.uid}:${k(m)}`}
                target={{ scope: "member", uid: m.uid }}
                budget={m}
                title={m.label ?? m.uid}
                description={m.uid === viewerUid ? "(you)" : roleOf.get(m.uid) === "owner" ? "owner" : undefined}
                editable={canEdit && !lock}
                readOnlyReason={canEdit ? lock : undefined}
                testId={`budget-member-${m.uid}`}
              />
            );
          })}
          {canEdit && (
            <AddBudget
              label="Member"
              options={memberOptions}
              toTarget={(uid) => ({ scope: "member", uid })}
              testId="add-member-budget"
            />
          )}
        </UsageSection>
      )}

      <UsageSection
        title="Projects"
        description={`Caps everyone's spend on one project. Each stays at or below the ${layer.toLowerCase()}'s budget.`}
        testId="budget-projects"
      >
        {data.projects.length === 0 && !canEdit && (
          <p className="text-sm text-muted-foreground">No project has a budget.</p>
        )}
        {data.projects.map((p) => (
          <BudgetEditor
            key={`${p.slug}:${k(p)}`}
            target={{ scope: "project", slug: p.slug }}
            budget={p}
            title={p.name}
            editable={canEdit}
            testId={`budget-project-${p.slug}`}
          />
        ))}
        {canEdit && (
          <AddBudget
            label="Project"
            options={projectOptions}
            toTarget={(slug) => ({ scope: "project", slug })}
            testId="add-project-budget"
          />
        )}
      </UsageSection>

      <UsageSection title="This month's alerts" description="Each threshold fires once per budget and month.">
        <Alerts alerts={data.alerts} orgLabel={layer} />
      </UsageSection>
    </div>
  );
}
