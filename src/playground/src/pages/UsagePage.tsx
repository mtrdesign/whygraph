import { PageContainer } from "../components/layout/PageContainer";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  membersApi,
  portalApi,
  portalKey,
  projectApi,
  usageApi,
  type UsageGroup,
  type UsageGroupRow,
  type UsageQuery,
} from "../api";
import { BudgetsTab } from "../components/usage/BudgetsTab";
import { BreakdownTable } from "../components/usage/BreakdownTable";
import { CallsTable, useCallsQuery } from "../components/usage/CallsTable";
import { PricesTab } from "../components/usage/PricesTab";
import { DailyCostChart } from "../components/usage/UsageChart";
import {
  ChoiceSelect,
  CsvButton,
  EstimatedNote,
  RangePicker,
  SpendBar,
  SplitLine,
  StatTile,
  UnpricedNote,
  UsageError,
  UsageSection,
} from "../components/usage/parts";
import { TabsSelect } from "../components/layout/TabsSelect";
import { Field } from "../components/portal/Field";
import { TableSkeleton } from "../components/state/Skeletons";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Skeleton } from "../components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useChatSessions } from "../lib/chatSessions";
import { formatNumber, formatPct, formatTokens, formatUsd } from "../lib/format";
import { canAdmin, canOwn, isProduction, usePortalState, useRole } from "../lib/identity";
import { inheritedLayerLabel } from "../lib/labels";
import { runTitle } from "../lib/scanFormat";
import { formatRange, monthRange, type UsageRange } from "../lib/usageRange";

// ---- the address ------------------------------------------------------------------

export type UsageTab = "overview" | "projects" | "members" | "models" | "machines" | "calls" | "budgets" | "prices";

const TABS: { key: UsageTab; label: string; production?: boolean }[] = [
  { key: "overview", label: "Overview" },
  { key: "projects", label: "Projects" },
  { key: "members", label: "Members", production: true },
  { key: "models", label: "Models" },
  { key: "machines", label: "Machines", production: true },
  { key: "calls", label: "Calls" },
  { key: "budgets", label: "Budgets" },
  { key: "prices", label: "Prices" },
];

/** `/usage`'s query: the tab, the range (`to` exclusive) and the Calls filters. */
export interface UsageSearch {
  tab?: UsageTab;
  from?: string;
  to?: string;
  project?: string;
  member?: string;
  task?: string;
  source?: string;
  model?: string;
  scan_run?: string;
  chat_session?: string;
  sort?: "time" | "cost";
}

/** A drill-down's query: just the range. */
export interface RangeSearch {
  from?: string;
  to?: string;
}

const str = (v: unknown) => (typeof v === "string" && v !== "" ? v : undefined);
const day = (v: unknown) => (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : undefined);
const id = (v: unknown) => (typeof v === "string" && /^\d+$/.test(v) ? v : undefined);

export function validateRangeSearch(search: Record<string, unknown>): RangeSearch {
  return { from: day(search.from), to: day(search.to) };
}

export function validateUsageSearch(search: Record<string, unknown>): UsageSearch {
  return {
    tab: TABS.find((t) => t.key === search.tab)?.key,
    ...validateRangeSearch(search),
    project: str(search.project),
    member: str(search.member),
    task: str(search.task),
    source: str(search.source),
    model: str(search.model),
    scan_run: id(search.scan_run),
    chat_session: id(search.chat_session),
    sort: search.sort === "cost" ? "cost" : undefined,
  };
}

/** The API filters a search carries (the range and the Calls filters). */
export function callsQuery(search: UsageSearch): UsageQuery {
  return {
    from: search.from,
    to: search.to,
    project: search.project,
    member: search.member,
    task: search.task,
    source: search.source,
    model: search.model,
    scan_run: search.scan_run ? Number(search.scan_run) : undefined,
    chat_session: search.chat_session && search.project ? Number(search.chat_session) : undefined,
    sort: search.sort,
  };
}

const TASKS = [
  { value: "analyze", label: "Descriptions" },
  { value: "rationale", label: "Rationale cards" },
  { value: "chat", label: "Chat" },
];
const SOURCES = [
  { value: "scan", label: "Scans" },
  { value: "explorer", label: "Explorer" },
  { value: "chat", label: "Chat" },
  { value: "mcp", label: "MCP (local agents)" },
  { value: "agent", label: "Agents (linked portals)" },
];

/** A task or source key as people read it. */
export function usageKeyLabel(group: UsageGroup, key: string): string {
  const list = group === "task" ? TASKS : group === "source" ? SOURCES : [];
  return list.find((t) => t.value === key)?.label ?? key;
}

// ---- shared hooks -------------------------------------------------------------------

/** The slugs of the projects the viewer can open (a call of any other project links nowhere). */
export function useOpenableProjects(): Set<string> {
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  return new Set((projects.data?.projects ?? []).map((p) => p.slug));
}

function useUsageReport(scope: "org" | "me", query: UsageQuery) {
  return useQuery({
    queryKey: portalKey("usage", scope, "report", query),
    queryFn: () => usageApi(scope).report(query),
  });
}

function rangeOf(search: RangeSearch): Partial<UsageRange> {
  return { from: search.from, to: search.to };
}

// ---- Overview ---------------------------------------------------------------------

/** The Overview while it loads: four tiles, the notes, the chart and the two top-5 tables, in their shapes (ER-5). */
function OverviewSkeleton() {
  const card = "flex flex-col gap-4 rounded-xl border border-border bg-card p-4 shadow-card sm:p-5";
  return (
    <div className="flex flex-col gap-6" data-testid="usage-overview-loading" aria-busy="true">
      <span className="sr-only" role="status">
        Loading the usage report
      </span>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="flex flex-col gap-1.5 rounded-lg border border-border bg-card px-3 py-2.5 shadow-card">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-7 w-24" />
            <Skeleton className="h-3 w-16" />
          </div>
        ))}
      </div>
      <div className="flex flex-col gap-1.5">
        <Skeleton className="h-3 w-72 max-w-full" />
        <Skeleton className="h-3 w-full" />
      </div>
      <Skeleton className="h-[260px] w-full rounded-xl" />
      <div className="grid gap-6 md:grid-cols-2">
        {[0, 1].map((i) => (
          <div key={i} className={card}>
            <div className="flex flex-col gap-1.5">
              <Skeleton className="h-4 w-28" />
              <Skeleton className="h-3 w-40" />
            </div>
            <TableSkeleton rows={3} cols={3} />
          </div>
        ))}
      </div>
    </div>
  );
}

function Overview({ toCalls }: { toCalls: (filter: UsageSearch) => UsageSearch }) {
  const state = usePortalState().data;
  const usage = state?.usage;
  const layer = inheritedLayerLabel(isProduction(state) ? "production" : "local", true);
  const thisMonth = useUsageReport("org", { group: "project" });
  const models = useUsageReport("org", { group: "model" });
  const lastMonth = useUsageReport("org", monthRange(-1));

  if (thisMonth.isLoading) return <OverviewSkeleton />;
  if (thisMonth.isError || !thisMonth.data) {
    return (
      <UsageError
        error={thisMonth.error}
        what="this usage report"
        title="Couldn't load the usage report"
        onRetry={() => void thisMonth.refetch()}
      />
    );
  }
  const cur = thisMonth.data;
  const last = lastMonth.data?.totals;
  const org = usage?.org;
  const linkProject = (row: UsageGroupRow) =>
    row.key === null ? null : (
      <Link to="/usage" search={toCalls({ project: String(row.key) })} className="text-primary-text hover:underline">
        {row.label}
      </Link>
    );
  const linkModel = (row: UsageGroupRow) =>
    row.key === null ? null : (
      <Link to="/usage" search={toCalls({ model: String(row.key) })} className="font-mono text-primary-text hover:underline">
        {row.label}
      </Link>
    );

  return (
    <div className="flex flex-col gap-6" data-testid="usage-overview">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile
          testId="tile-this-month"
          label={
            <>
              This month <Badge variant="outline">estimated</Badge>
            </>
          }
          value={formatUsd(cur.totals.cost_usd)}
          detail={`${formatNumber(cur.totals.calls)} calls`}
        />
        <StatTile
          testId="tile-last-month"
          label="Last month"
          value={last ? formatUsd(last.cost_usd) : "-"}
          detail={last ? `${formatNumber(last.calls)} calls` : undefined}
        />
        <StatTile
          label="Tokens in / out"
          value={`${formatTokens(cur.totals.input_tokens)} / ${formatTokens(cur.totals.output_tokens)}`}
          detail={
            cur.totals.cache_read_tokens ? `${formatTokens(cur.totals.cache_read_tokens)} read from cache` : undefined
          }
        />
        <StatTile
          testId="tile-budget"
          label={`${layer} budget`}
          value={org?.budget_usd != null ? formatPct(org.pct) : "-"}
          detail={
            org?.budget_usd != null ? (
              <span className="flex flex-col gap-1">
                <span>
                  of {formatUsd(org.budget_usd)}
                  {org.hard_stop ? ", hard stop" : ""}
                </span>
                <SpendBar pct={org.pct} />
              </span>
            ) : (
              <Link to="/usage" search={{ tab: "budgets" }} className="text-primary-text hover:underline">
                None - set one
              </Link>
            )
          }
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <SplitLine split={cur.split} />
        <UnpricedNote calls={cur.totals.unpriced_calls} />
        <EstimatedNote />
      </div>
      <DailyCostChart series={cur.series} />
      <div className="grid gap-6 md:grid-cols-2">
        <UsageSection title="Top projects" description="This month, costliest first.">
          <BreakdownTable group="project" rows={cur.groups} limit={5} compact link={linkProject} />
        </UsageSection>
        <UsageSection title="Top models" description="This month, by the model the provider served.">
          {models.isLoading ? (
            <TableSkeleton rows={3} cols={3} label="Loading the top models" />
          ) : (
            <BreakdownTable group="model" rows={models.data?.groups ?? []} limit={5} compact link={linkModel} />
          )}
        </UsageSection>
      </div>
    </div>
  );
}

// ---- one breakdown -------------------------------------------------------------------

const GROUP_INTRO: Partial<Record<UsageGroup, string>> = {
  project: "What each project's calls cost. A removed project keeps its rows under its old slug.",
  member:
    "Who triggered the spend: interactive is chat, Explorer Generate, agent calls and on-demand descriptions; scans are the full scans they started. This shows who started the spend, not who gained from it, so it is not a ranking. System covers webhook, reconcile and hook scans.",
  model: "Spend per model, as the provider served it.",
  machine:
    "Agent calls through a linked portal, per machine. Everything made in this portal itself is under Portal.",
};

function GroupTab({
  group,
  search,
  setRange,
  link,
}: {
  group: UsageGroup;
  search: UsageSearch;
  setRange: (range: Partial<UsageRange>) => void;
  link?: (row: UsageGroupRow) => ReactNode | null;
}) {
  const range = rangeOf(search);
  const report = useUsageReport("org", { ...range, group });
  // An empty breakdown has nothing to export.
  const empty = !!report.data && report.data.groups.length === 0;
  return (
    <div className="flex flex-col gap-4" data-testid={`usage-tab-${group}`}>
      <p className="text-[13px] text-muted-foreground">{GROUP_INTRO[group]}</p>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <RangePicker range={range} onChange={setRange} />
        {!empty && <CsvButton download={() => usageApi("org").csv({ ...range, group })} testId={`csv-${group}`} />}
      </div>
      {report.isLoading && <TableSkeleton rows={5} cols={4} label="Loading this breakdown" />}
      {report.isError && (
        <UsageError
          error={report.error}
          what="this usage report"
          title="Couldn't load this breakdown"
          onRetry={() => void report.refetch()}
        />
      )}
      {report.data && (
        <>
          <p className="text-xs text-muted-foreground">
            {formatRange(report.data.range)}: {formatUsd(report.data.totals.cost_usd)} estimated over{" "}
            {formatNumber(report.data.totals.calls)} calls.
          </p>
          <BreakdownTable group={group} rows={report.data.groups} link={link} />
        </>
      )}
    </div>
  );
}

// ---- Calls ------------------------------------------------------------------------

/** How long the Model box waits after the last keystroke before it filters. */
const MODEL_DEBOUNCE_MS = 350;

/** The "This scan: ..." / "This chat: ..." chips name what they filter to, never an id (USE-5). */
function useChipLabels(search: UsageSearch, viewerUid: string | undefined) {
  const first = useCallsQuery("org", callsQuery(search));
  const rows = first.data?.pages.flatMap((p) => p.items) ?? [];
  const runId = search.scan_run ? Number(search.scan_run) : null;
  const sessionId = search.chat_session && search.project ? Number(search.chat_session) : null;
  const runSlug = search.project ?? rows.find((c) => c.scan_run_id === runId)?.project_slug ?? null;
  const run = useQuery({
    queryKey: ["usage", "chip", "scan", runSlug, runId],
    queryFn: () => projectApi(runSlug as string).scan(runId as number),
    enabled: runId !== null && !!runSlug,
    retry: false,
  });
  // A chat is its owner's: another member's session has no title for the viewer.
  const owner = rows.find((c) => c.chat_session_id === sessionId)?.user_uid ?? null;
  const foreign = sessionId !== null && owner !== null && !!viewerUid && owner !== viewerUid;
  const sessions = useChatSessions(search.project ?? "", sessionId !== null && !foreign && !!search.project);
  const title = sessions.data?.find((s) => s.id === sessionId)?.title;
  return {
    scan: runId === null ? null : run.data ? `This scan: ${runTitle(run.data)}` : "This scan",
    chat: sessionId === null ? null : foreign ? "Chat (another member)" : title ? `This chat: ${title}` : "This chat",
  };
}

function CallsTab({
  search,
  production,
  apply,
}: {
  search: UsageSearch;
  production: boolean;
  apply: (next: UsageSearch) => void;
}) {
  const state = usePortalState().data;
  const openable = useOpenableProjects();
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  const members = useQuery({ queryKey: portalKey("members"), queryFn: membersApi.list, enabled: production });
  const query = callsQuery(search);
  const chips = useChipLabels(search, state?.user?.uid);

  // Every filter applies as it changes; only the Model text waits for typing to stop.
  const latest = useRef({ search, apply });
  latest.current = { search, apply };
  const [model, setModel] = useState(search.model ?? "");
  useEffect(() => setModel(search.model ?? ""), [search.model]);
  useEffect(() => {
    const value = model.trim() || undefined;
    if (value === latest.current.search.model) return;
    const timer = setTimeout(() => latest.current.apply({ ...latest.current.search, model: value }), MODEL_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [model]);

  const select = (label: string, key: "project" | "member" | "task" | "source", options: { value: string; label: string }[]) => (
    <Field label={label} className="w-40 max-sm:w-full">
      {(p) => (
        <ChoiceSelect
          {...p}
          value={search[key] ?? ""}
          onChange={(value) =>
            apply({
              ...search,
              [key]: value || undefined,
              // A session id means nothing without its project.
              ...(key === "project" ? { chat_session: undefined } : {}),
            })
          }
          options={[{ value: "", label: "All" }, ...options]}
        />
      )}
    </Field>
  );
  const projectOptions = (projects.data?.projects ?? []).map((p) => ({ value: p.slug, label: p.name }));
  // A filter on a removed project (from a breakdown link) stays selectable.
  if (search.project && !projectOptions.some((o) => o.value === search.project)) {
    projectOptions.push({ value: search.project, label: search.project });
  }
  const memberOptions = [
    ...(members.data ?? []).map((m) => ({ value: m.uid, label: m.display_name || m.github_login || m.uid })),
    { value: "system", label: "System" },
  ];
  const active: { label: string; clear: UsageSearch }[] = [];
  if (chips.scan) active.push({ label: chips.scan, clear: { ...search, scan_run: undefined } });
  if (chips.chat) active.push({ label: chips.chat, clear: { ...search, chat_session: undefined } });

  return (
    <div className="flex flex-col gap-4" data-testid="usage-tab-calls">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <RangePicker range={rangeOf(search)} onChange={(r) => apply({ ...search, from: r.from, to: r.to })} />
        <CsvButton download={() => usageApi("org").csv(query)} testId="csv-calls" />
      </div>
      <div className="flex flex-wrap items-end gap-3" role="group" aria-label="Filters" data-testid="calls-filters">
        {select("Project", "project", projectOptions)}
        {production && select("Member", "member", memberOptions)}
        {select("Task", "task", TASKS)}
        {select("Source", "source", SOURCES)}
        <Field label="Model" className="w-44 max-sm:w-full">
          {(p) => <Input {...p} placeholder="e.g. claude-sonnet-4-5" value={model} onChange={(e) => setModel(e.target.value)} />}
        </Field>
        <Field label="Sort" className="w-36 max-sm:w-full">
          {(p) => (
            <ChoiceSelect
              {...p}
              value={search.sort ?? "time"}
              onChange={(value) => apply({ ...search, sort: value === "cost" ? "cost" : undefined })}
              options={[
                { value: "time", label: "Newest first" },
                { value: "cost", label: "Costliest first" },
              ]}
            />
          )}
        </Field>
      </div>
      {active.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {active.map((c) => (
            <Button key={c.label} type="button" size="xs" variant="secondary" onClick={() => apply(c.clear)}>
              {c.label} ×
            </Button>
          ))}
        </div>
      )}
      <CallsTable scope="org" query={query} viewerUid={state?.user?.uid} openable={openable} showWho={production} />
    </div>
  );
}

// ---- the page -----------------------------------------------------------------------

/**
 * `/usage` (plan section 4.13): what the org's LLM keys spent, on what, and because
 * of whom. For `org.usage` callers (owners, org admins, readers; local mode's
 * user); a production member is sent to `/usage/me` by the route. Members and
 * Machines are production tabs; Budgets and Prices are editable for owners and
 * org admins (`org.budgets`) and read-only for a reader.
 */
export function UsagePage() {
  const state = usePortalState().data;
  const role = useRole();
  const production = isProduction(state);
  const search = useSearch({ strict: false }) as UsageSearch;
  const navigate = useNavigate();
  const tabs = TABS.filter((t) => production || !t.production);
  const tab = tabs.find((t) => t.key === search.tab)?.key ?? "overview";

  const go = (next: UsageSearch) => void navigate({ to: "/usage", search: next });
  const setRange = (range: Partial<UsageRange>) => go({ ...search, from: range.from, to: range.to });
  // A breakdown row's Calls view: the same range, one filter.
  const toCalls = (filter: UsageSearch): UsageSearch => ({ tab: "calls", from: search.from, to: search.to, ...filter });
  const callsLink = (field: "project" | "model") => (row: UsageGroupRow) =>
    row.key === null ? null : (
      <Link
        to="/usage"
        search={toCalls({ [field]: String(row.key) })}
        className={field === "model" ? "font-mono text-primary-text hover:underline" : "text-primary-text hover:underline"}
      >
        {row.label}
      </Link>
    );
  const memberLink = (row: UsageGroupRow) => {
    if (row.key !== null) {
      return (
        <Link
          to="/usage/members/$uid"
          params={{ uid: String(row.key) }}
          search={{ from: search.from, to: search.to }}
          className="text-primary-text hover:underline"
        >
          {row.label}
        </Link>
      );
    }
    // The System actor has its own filter; a deleted member's rows have none.
    return row.label === "System" ? (
      <Link to="/usage" search={toCalls({ member: "system" })} className="text-primary-text hover:underline">
        System
      </Link>
    ) : null;
  };

  return (
    <PageContainer width="default" className="flex flex-col gap-6" data-testid="usage-page">
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <h1 className="text-[22px] font-semibold tracking-tight">Usage & cost</h1>
          <p className="text-[13px] text-muted-foreground">
            {production
              ? `What ${state?.org?.name ?? "this organization"}'s LLM keys are spending, on what, and because of whom.`
              : "What this portal's LLM keys are spending, and on what."}{" "}
            Counts and cost only; no prompt or chat content is kept.
          </p>
        </div>
        {state?.usage?.me && (
          <Link to="/usage/me" className="text-sm text-primary-text hover:underline">
            My usage
          </Link>
        )}
      </div>
      <Tabs value={tab} onValueChange={(value) => go({ ...search, tab: value as UsageTab })} className="gap-5">
        {/* Eight tabs do not fit a phone: a select stands in below `sm` (PH-6). */}
        <TabsSelect
          label="Usage view"
          value={tab}
          onValueChange={(value) => go({ ...search, tab: value as UsageTab })}
          tabs={tabs.map((t) => ({ value: t.key, label: t.label }))}
        />
        <TabsList variant="scrollable" className="max-sm:hidden">
          {tabs.map((t) => (
            <TabsTrigger key={t.key} value={t.key} className="flex-none text-[13px]">
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="overview">{tab === "overview" && <Overview toCalls={toCalls} />}</TabsContent>
        <TabsContent value="projects">
          {tab === "projects" && <GroupTab group="project" search={search} setRange={setRange} link={callsLink("project")} />}
        </TabsContent>
        {production && (
          <TabsContent value="members">
            {tab === "members" && <GroupTab group="member" search={search} setRange={setRange} link={memberLink} />}
          </TabsContent>
        )}
        <TabsContent value="models">
          {tab === "models" && <GroupTab group="model" search={search} setRange={setRange} link={callsLink("model")} />}
        </TabsContent>
        {production && (
          <TabsContent value="machines">
            {tab === "machines" && <GroupTab group="machine" search={search} setRange={setRange} />}
          </TabsContent>
        )}
        <TabsContent value="calls">
          {tab === "calls" && (
            <CallsTab
              search={search}
              production={production}
              apply={(next) => go({ ...next, tab: "calls" })}
            />
          )}
        </TabsContent>
        <TabsContent value="budgets">
          {tab === "budgets" && (
            <BudgetsTab
              canEdit={canAdmin(role)}
              isOwner={canOwn(role)}
              production={production}
              viewerUid={state?.user?.uid}
            />
          )}
        </TabsContent>
        <TabsContent value="prices">{tab === "prices" && <PricesTab canEdit={canAdmin(role)} />}</TabsContent>
      </Tabs>
    </PageContainer>
  );
}
