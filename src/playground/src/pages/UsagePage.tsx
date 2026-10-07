import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  membersApi,
  portalApi,
  portalKey,
  usageApi,
  type UsageGroup,
  type UsageGroupRow,
  type UsageQuery,
} from "../api";
import { BudgetsTab } from "../components/usage/BudgetsTab";
import { BreakdownTable } from "../components/usage/BreakdownTable";
import { CallsTable } from "../components/usage/CallsTable";
import { PricesTab } from "../components/usage/PricesTab";
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
import { Field, nativeSelectClass } from "../components/portal/Field";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Skeleton } from "../components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { authMessage } from "../lib/authErrors";
import { formatPct, formatTokens, formatUsd } from "../lib/format";
import { canAdmin, canOwn, isProduction, usePortalState, useRole } from "../lib/identity";
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

function Overview({ toCalls }: { toCalls: (filter: UsageSearch) => UsageSearch }) {
  const usage = usePortalState().data?.usage;
  const thisMonth = useUsageReport("org", { group: "project" });
  const models = useUsageReport("org", { group: "model" });
  const lastMonth = useUsageReport("org", monthRange(-1));

  if (thisMonth.isLoading) return <Skeleton className="h-60" />;
  if (thisMonth.isError || !thisMonth.data) {
    return <p className="text-sm text-destructive">Failed to load: {authMessage(thisMonth.error)}</p>;
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
          detail={`${cur.totals.calls.toLocaleString("en-US")} calls`}
        />
        <StatTile
          testId="tile-last-month"
          label="Last month"
          value={last ? formatUsd(last.cost_usd) : "-"}
          detail={last ? `${last.calls.toLocaleString("en-US")} calls` : undefined}
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
          label="Organization budget"
          value={org?.budget_usd != null ? formatPct(org.pct) : "None"}
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
              "Set one on the Budgets tab"
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
            <Skeleton className="h-24" />
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
    "Who triggered the spend: interactive is chat, Explorer Generate, agent calls and on-demand descriptions; scans are the full scans they started. Triggered is not benefited, so this is not a ranking. System covers webhook, reconcile and hook scans.",
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
  return (
    <div className="flex flex-col gap-4" data-testid={`usage-tab-${group}`}>
      <p className="text-[13px] text-muted-foreground">{GROUP_INTRO[group]}</p>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <RangePicker range={range} onChange={setRange} />
        <CsvButton download={() => usageApi("org").csv({ ...range, group })} testId={`csv-${group}`} />
      </div>
      {report.isLoading && <Skeleton className="h-32" />}
      {report.isError && <p className="text-sm text-destructive">Failed to load: {authMessage(report.error)}</p>}
      {report.data && (
        <>
          <p className="text-xs text-muted-foreground">
            {formatRange(report.data.range)}: {formatUsd(report.data.totals.cost_usd)} estimated over{" "}
            {report.data.totals.calls.toLocaleString("en-US")} calls.
          </p>
          <BreakdownTable group={group} rows={report.data.groups} link={link} />
        </>
      )}
    </div>
  );
}

// ---- Calls ------------------------------------------------------------------------

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
  const [draft, setDraft] = useState({
    project: search.project ?? "",
    member: search.member ?? "",
    task: search.task ?? "",
    source: search.source ?? "",
    model: search.model ?? "",
    sort: search.sort ?? "time",
  });
  const query = callsQuery(search);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    apply({
      ...search,
      project: draft.project || undefined,
      member: draft.member || undefined,
      task: draft.task || undefined,
      source: draft.source || undefined,
      model: draft.model.trim() || undefined,
      sort: draft.sort === "cost" ? "cost" : undefined,
      // A session id means nothing without its project.
      chat_session: draft.project && draft.project === search.project ? search.chat_session : undefined,
    });
  };
  const select = (label: string, key: "project" | "member" | "task" | "source", options: { value: string; label: string }[]) => (
    <Field label={label} className="w-40">
      {(p) => (
        <select {...p} className={nativeSelectClass} value={draft[key]} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}>
          <option value="">All</option>
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
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
  const chips: { label: string; clear: UsageSearch }[] = [];
  if (search.scan_run) chips.push({ label: `Scan run #${search.scan_run}`, clear: { ...search, scan_run: undefined } });
  if (search.chat_session && search.project) {
    chips.push({ label: `Chat session #${search.chat_session}`, clear: { ...search, chat_session: undefined } });
  }

  return (
    <div className="flex flex-col gap-4" data-testid="usage-tab-calls">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <RangePicker range={rangeOf(search)} onChange={(r) => apply({ ...search, from: r.from, to: r.to })} />
        <CsvButton download={() => usageApi("org").csv(query)} testId="csv-calls" />
      </div>
      <form onSubmit={submit} className="flex flex-wrap items-end gap-3" data-testid="calls-filters">
        {select("Project", "project", projectOptions)}
        {production && select("Member", "member", memberOptions)}
        {select("Task", "task", TASKS)}
        {select("Source", "source", SOURCES)}
        <Field label="Model" className="w-44">
          {(p) => <Input {...p} placeholder="claude-sonnet-4-5" value={draft.model} onChange={(e) => setDraft({ ...draft, model: e.target.value })} />}
        </Field>
        <Field label="Sort" className="w-36">
          {(p) => (
            <select {...p} className={nativeSelectClass} value={draft.sort} onChange={(e) => setDraft({ ...draft, sort: e.target.value as "time" | "cost" })}>
              <option value="time">Newest first</option>
              <option value="cost">Costliest first</option>
            </select>
          )}
        </Field>
        <Button type="submit" variant="outline">
          Filter
        </Button>
      </form>
      {chips.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {chips.map((c) => (
            <Button key={c.label} type="button" size="xs" variant="secondary" onClick={() => apply(c.clear)}>
              {c.label} ×
            </Button>
          ))}
        </div>
      )}
      <CallsTable scope="org" query={query} viewerUid={state?.user?.uid} openable={openable} />
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
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-6 sm:p-8" data-testid="usage-page">
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <h1 className="text-[22px] font-semibold tracking-tight">Usage & cost</h1>
          <p className="text-[13px] text-muted-foreground">
            What {state?.org?.name ?? "this organization"}'s LLM keys are spending, on what, and because of whom. Counts
            and cost only; no prompt or chat content is kept.
          </p>
        </div>
        {state?.usage?.me && (
          <Link to="/usage/me" className="text-sm text-primary-text hover:underline">
            My usage
          </Link>
        )}
      </div>
      <Tabs value={tab} onValueChange={(value) => go({ ...search, tab: value as UsageTab })} className="gap-5">
        <TabsList
          variant="line"
          // Scrolls sideways on a phone; the bottom padding keeps the active underline inside the clip.
          className="w-full justify-start overflow-x-auto overflow-y-hidden border-b border-border pb-[7px] group-data-horizontal/tabs:h-10"
        >
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
              // Re-seed the filter form when a link changes the filters.
              key={JSON.stringify(callsQuery(search))}
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
    </div>
  );
}
