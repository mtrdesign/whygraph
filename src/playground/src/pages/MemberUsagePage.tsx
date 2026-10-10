import { PageContainer } from "../components/layout/PageContainer";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { membersApi, portalKey, usageApi, type UsageGroup, type UsageGroupRow, type UsageQuery } from "../api";
import { BreakdownTable } from "../components/usage/BreakdownTable";
import { CallsTable } from "../components/usage/CallsTable";
import { DailyCostChart } from "../components/usage/UsageChart";
import {
  CsvButton,
  EstimatedNote,
  RangePicker,
  SpendBar,
  SplitLine,
  StatTile,
  UnpricedNote,
  UsageSection,
} from "../components/usage/parts";
import { Badge } from "../components/ui/badge";
import { Skeleton } from "../components/ui/skeleton";
import { ErrorState } from "../components/state/ErrorState";
import { formatNumber, formatPct, formatTokens, formatUsd } from "../lib/format";
import { usePortalState } from "../lib/identity";
import { formatMonth, formatRange, formatResetsAt, type UsageRange } from "../lib/usageRange";
import { useOpenableProjects, usageKeyLabel, type RangeSearch } from "./UsagePage";

function Breakdown({
  title,
  scope,
  query,
  group,
}: {
  title: string;
  scope: "org" | "me";
  query: UsageQuery;
  group: UsageGroup;
}) {
  const report = useQuery({
    queryKey: portalKey("usage", scope, "report", { ...query, group }),
    queryFn: () => usageApi(scope).report({ ...query, group }),
  });
  const pretty = (row: UsageGroupRow) => (row.key === null ? null : usageKeyLabel(group, String(row.key)));
  return (
    <UsageSection title={title}>
      {report.isLoading && <Skeleton className="h-20" />}
      {report.isError && <ErrorState error={report.error} context="usage" onRetry={() => void report.refetch()} />}
      {report.data && (
        <BreakdownTable group={group} rows={report.data.groups} compact link={group === "task" ? pretty : undefined} />
      )}
    </UsageSection>
  );
}

/** The caller's own budget this month (`state.usage.me`), on My usage. */
function MyBudget() {
  const usage = usePortalState().data?.usage;
  const me = usage?.me;
  if (!usage || !me) return null;
  return (
    <UsageSection
      title={`Your budget for ${formatMonth(usage.month)}`}
      description={`Resets ${formatResetsAt(usage.resets_at)}.`}
      testId="my-budget"
    >
      {me.budget_usd === null ? (
        <p className="text-sm text-muted-foreground">
          You have spent {formatUsd(me.spent_usd)} this month. No member budget applies to you.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <p className="text-sm">
            {formatUsd(me.spent_usd)} of {formatUsd(me.budget_usd)} ({formatPct(me.pct)})
            {me.hard_stop && <span className="text-muted-foreground">, hard stop</span>}
          </p>
          <SpendBar pct={me.pct} label="Spent of your budget" />
          {me.blocked && (
            <p className="text-sm text-destructive">
              You've reached your monthly budget. You can still read everything that's already generated.
            </p>
          )}
        </div>
      )}
    </UsageSection>
  );
}

/**
 * One member's usage: `/usage/members/$uid` for `org.usage` callers, or the
 * caller's own at `/usage/me` (production). Spend over time; by project, task and
 * machine; the interactive / scans split; the most expensive calls.
 */
export function MemberUsagePage({ uid }: { uid?: string }) {
  const state = usePortalState().data;
  const search = useSearch({ strict: false }) as RangeSearch;
  const navigate = useNavigate();
  const openable = useOpenableProjects();
  const scope: "org" | "me" = uid ? "org" : "me";
  const range: Partial<UsageRange> = { from: search.from, to: search.to };
  // `/me` routes are forced to the caller server-side; `member` is for the org view only.
  const query: UsageQuery = uid ? { ...range, member: uid } : range;

  const members = useQuery({ queryKey: portalKey("members"), queryFn: membersApi.list, enabled: !!uid });
  const member = uid ? members.data?.find((m) => m.uid === uid) : undefined;
  const report = useQuery({
    queryKey: portalKey("usage", scope, "report", { ...query, group: "project" }),
    queryFn: () => usageApi(scope).report({ ...query, group: "project" }),
  });

  const setRange = (r: Partial<UsageRange>) => {
    const next = { from: r.from, to: r.to };
    void (uid
      ? navigate({ to: "/usage/members/$uid", params: { uid }, search: next })
      : navigate({ to: "/usage/me", search: next }));
  };
  const title = uid ? (member?.display_name ?? member?.github_login ?? uid) : "My usage";

  return (
    <PageContainer width="default" className="flex flex-col gap-6" data-testid="member-usage-page">
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <h1 className="text-[22px] font-semibold tracking-tight">
            {title}
            {uid && member?.github_login && (
              <span className="ml-2 font-mono text-sm font-normal text-muted-foreground">@{member.github_login}</span>
            )}
            {uid && uid === state?.user?.uid && <span className="font-normal text-muted-foreground"> (you)</span>}
          </h1>
          <p className="text-[13px] text-muted-foreground">
            {uid
              ? "The LLM spend this member triggered, in "
              : "The LLM spend you triggered, in "}
            {state?.org?.name ?? "this organization"}. Counts and cost only; no prompt or chat content is kept.
          </p>
        </div>
        {uid && (
          <Link
            to="/usage"
            search={{ tab: "calls", member: uid, from: search.from, to: search.to }}
            className="text-sm text-primary-text hover:underline"
          >
            All their calls
          </Link>
        )}
      </div>

      {!uid && <MyBudget />}

      <div className="flex flex-wrap items-end justify-between gap-3">
        <RangePicker range={range} onChange={setRange} />
        <CsvButton download={() => usageApi(scope).csv(query)} testId="csv-member" />
      </div>

      {report.isLoading && <Skeleton className="h-40" />}
      {report.isError && <ErrorState error={report.error} context="usage" onRetry={() => void report.refetch()} />}
      {report.data && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile
              testId="member-tile-cost"
              label={
                <>
                  Cost <Badge variant="outline">estimated</Badge>
                </>
              }
              value={formatUsd(report.data.totals.cost_usd)}
              detail={formatRange(report.data.range)}
            />
            <StatTile label="Calls" value={formatNumber(report.data.totals.calls)} />
            <StatTile label="Tokens in" value={formatTokens(report.data.totals.input_tokens)} />
            <StatTile label="Tokens out" value={formatTokens(report.data.totals.output_tokens)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <SplitLine split={report.data.split} />
            <p className="text-xs text-muted-foreground">
              Interactive is chat, Explorer Generate, agent calls and on-demand descriptions; scans are the full scans{" "}
              {uid ? "they" : "you"} started, which benefit everyone on the project.
            </p>
            <UnpricedNote calls={report.data.totals.unpriced_calls} />
            <EstimatedNote />
          </div>
          <DailyCostChart series={report.data.series} title="Spend over time" />
          <div className="grid gap-6 md:grid-cols-3">
            <UsageSection title="By project">
              <BreakdownTable group="project" rows={report.data.groups} compact />
            </UsageSection>
            <Breakdown title="By task" scope={scope} query={query} group="task" />
            <Breakdown title="By machine" scope={scope} query={query} group="machine" />
          </div>
        </>
      )}

      <UsageSection title="Most expensive calls" description="In this range, costliest first." testId="expensive-calls">
        <CallsTable
          scope={scope}
          query={{ ...query, sort: "cost" }}
          viewerUid={state?.user?.uid}
          openable={openable}
          showWho={false}
        />
      </UsageSection>
    </PageContainer>
  );
}
