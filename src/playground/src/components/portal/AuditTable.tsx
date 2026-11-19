import { errorMessage } from "../../lib/apiErrors";
import { Fragment, useEffect, useState, type FormEvent } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { adminApi, auditApi, type AuditEventRow, type AuditFilters } from "../../api";
import {
  AUDIT_GROUPS,
  auditDetails,
  auditEventsIn,
  auditLabel,
  auditTarget,
  type AuditDetail,
} from "../../lib/auditEvents";
import { saveBlob } from "../../lib/download";
import { formatDateTime } from "../../lib/format";
import { ResponsiveTable, type Column } from "../layout/ResponsiveTable";
import { ErrorState } from "../state/ErrorState";
import { TableSkeleton } from "../state/Skeletons";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Field, nativeSelectClass } from "./Field";

type Page = { events: AuditEventRow[]; next: number | null };

/**
 * A details value that wraps between words and segments (after `@`, `.`, `/`,
 * `_` and `-`) before it would split one (only a segment wider than the whole
 * cell splits); an id is short, muted and mono, with the whole value in its title.
 */
function DetailValue({ detail }: { detail: Omit<AuditDetail, "label"> }) {
  const parts = detail.value.split(/(?<=[@./_-])/);
  return (
    <span
      title={detail.title}
      className={detail.mono ? "font-mono text-muted-foreground" : undefined}
      data-testid="audit-value"
    >
      {parts.map((part, i) => (
        <Fragment key={i}>
          {i > 0 && <wbr />}
          {part}
        </Fragment>
      ))}
    </span>
  );
}

/**
 * The audit columns over rows already loaded (Event, Organization, When, Actor, Target, Details).
 * `showOrg` adds the Organization column, first after Event, naming each event's org from `orgNames`.
 */
export function AuditRows({
  rows,
  admin,
  showOrg = false,
  orgNames,
}: {
  rows: AuditEventRow[];
  admin: boolean;
  showOrg?: boolean;
  orgNames?: Map<string, string>;
}) {
  const columns: Column<AuditEventRow>[] = [
    {
      key: "event",
      header: "Event",
      primary: true,
      cell: (r) => <span title={r.event}>{auditLabel(r.event)}</span>,
    },
    ...(showOrg
      ? [
          {
            key: "org",
            header: "Organization",
            cell: (r: AuditEventRow) => (r.org ? (orgNames?.get(r.org) ?? r.org) : "-"),
          },
        ]
      : []),
    {
      key: "when",
      header: "When",
      cell: (r) => <span className="whitespace-nowrap text-muted-foreground">{formatDateTime(r.created_at)}</span>,
    },
    { key: "actor", header: "Actor", cell: (r) => r.actor?.label ?? r.actor?.uid ?? "System" },
    {
      key: "target",
      header: "Target",
      cell: (r) => {
        const target = auditTarget(r);
        return target ? <DetailValue detail={target} /> : null;
      },
    },
    {
      key: "details",
      header: "Details",
      cell: (r) => {
        const pairs = auditDetails(r, admin);
        if (pairs.length === 0) return null;
        return (
          <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-xs">
            {pairs.map((d) => (
              <div key={d.label} className="contents">
                <dt className="text-muted-foreground">{d.label}</dt>
                <dd className="min-w-0 break-words">
                  <DetailValue detail={d} />
                </dd>
              </div>
            ))}
          </dl>
        );
      },
    },
  ];
  return (
    <ResponsiveTable columns={columns} rows={rows} rowKey={(r) => String(r.id)} rowTestId={(r) => `audit-${r.id}`} />
  );
}

/** The admin log's filters as the URL holds them; the org is a slug, `scope: "orgless"` means no organization. */
export type AuditUrlFilters = Pick<AuditFilters, "event" | "actor" | "from" | "to" | "org"> & { scope?: "orgless" };

const ORGLESS = "@orgless";

/**
 * Security events, newest first, with a grouped event select and actor / date filters and "Load more"
 * (keyset paging on the server's `next`). `scope` picks the endpoint: the org's log or the platform
 * administrator's cross-org log (plan 59, §4.9). The admin log gains an Organization filter and column,
 * unless `org` locks it to one org (the org detail's Audit tab). Both have a CSV download. With
 * `filters` and `onFilters` the applied filters live in the caller's URL; without them they are local.
 */
export function AuditTable({
  scope,
  filename = "whygraph-audit.csv",
  org,
  filters,
  onFilters,
}: {
  scope: "org" | "admin";
  filename?: string;
  org?: string;
  filters?: AuditUrlFilters;
  onFilters?: (next: AuditUrlFilters) => void;
}) {
  const [localApplied, setLocalApplied] = useState<AuditUrlFilters>({});
  const applied = filters ?? localApplied;
  const fromApplied = (f: AuditUrlFilters) => ({
    event: f.event ?? "",
    actor: f.actor ?? "",
    from: f.from ?? "",
    to: f.to ?? "",
    org: f.scope === "orgless" ? ORGLESS : (f.org ?? ""),
  });
  const [draft, setDraft] = useState(fromApplied(applied));
  const [csvError, setCsvError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  // The URL moved (a link such as "All events by this user"): the form follows it.
  const appliedKey = JSON.stringify(applied);
  useEffect(() => {
    setDraft(fromApplied(JSON.parse(appliedKey) as AuditUrlFilters));
  }, [appliedKey]);

  const orgFilter = scope === "admin" && org === undefined;
  const orgs = useQuery({ queryKey: ["@admin", "orgs"], queryFn: adminApi.orgs, enabled: scope === "admin" });
  const orgNames = new Map((orgs.data ?? []).map((o) => [o.slug, o.deleted_at ? `${o.name} (deleted)` : o.name]));

  const request: AuditFilters =
    scope === "admin"
      ? { ...applied, ...(org !== undefined ? { org } : {}), scope: applied.scope ?? "all" }
      : applied;
  const fetchPage = scope === "org" ? auditApi.org : auditApi.admin;
  const events = useInfiniteQuery<Page, Error, { pages: Page[] }, readonly unknown[], number | undefined>({
    queryKey: scope === "admin" ? ["@admin", "audit", request] : ["@audit", scope, request],
    queryFn: ({ pageParam }) => fetchPage({ ...request, before: pageParam }),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next ?? undefined,
  });

  const apply = (e: FormEvent) => {
    e.preventDefault();
    const next: AuditUrlFilters = {};
    for (const key of ["event", "actor", "from", "to"] as const) {
      if (draft[key].trim() !== "") next[key] = draft[key].trim();
    }
    if (orgFilter && draft.org === ORGLESS) next.scope = "orgless";
    else if (orgFilter && draft.org !== "") next.org = draft.org;
    if (onFilters) onFilters(next);
    else setLocalApplied(next);
  };
  const download = async () => {
    setCsvError(null);
    setDownloading(true);
    try {
      saveBlob(await (scope === "admin" ? auditApi.adminCsv(request) : auditApi.csv(request)), filename);
    } catch (err) {
      setCsvError(errorMessage(err));
    } finally {
      setDownloading(false);
    }
  };

  const rows = events.data?.pages.flatMap((p) => p.events) ?? [];
  return (
    <div className="flex flex-col gap-4" data-testid="audit-table">
      <form onSubmit={apply} className="flex flex-wrap items-end gap-3" data-testid="audit-filters">
        <Field label="Event" className="w-full sm:w-56">
          {(p) => (
            <select
              {...p}
              className={nativeSelectClass}
              value={draft.event}
              onChange={(e) => setDraft({ ...draft, event: e.target.value })}
            >
              <option value="">All events</option>
              {AUDIT_GROUPS.map((group) => (
                <optgroup key={group} label={group}>
                  {auditEventsIn(group).map((e) => (
                    <option key={e.event} value={e.event}>
                      {e.label}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          )}
        </Field>
        {orgFilter && (
          <Field label="Organization" className="w-full sm:w-56">
            {(p) => (
              <select
                {...p}
                className={nativeSelectClass}
                value={draft.org}
                onChange={(e) => setDraft({ ...draft, org: e.target.value })}
              >
                <option value="">All organizations</option>
                <option value={ORGLESS}>Platform events (no organization)</option>
                {draft.org !== "" && draft.org !== ORGLESS && !orgNames.has(draft.org) && (
                  <option value={draft.org}>{draft.org}</option>
                )}
                {[...orgNames].map(([slug, label]) => (
                  <option key={slug} value={slug}>
                    {label}
                  </option>
                ))}
              </select>
            )}
          </Field>
        )}
        <Field label="Actor" className="w-full sm:w-40">
          {(p) => <Input {...p} placeholder="@ben" value={draft.actor} onChange={(e) => setDraft({ ...draft, actor: e.target.value })} />}
        </Field>
        <Field label="From" className="w-full sm:w-40">
          {(p) => <Input {...p} type="date" value={draft.from} onChange={(e) => setDraft({ ...draft, from: e.target.value })} />}
        </Field>
        <Field label="To" className="w-full sm:w-40">
          {(p) => <Input {...p} type="date" value={draft.to} onChange={(e) => setDraft({ ...draft, to: e.target.value })} />}
        </Field>
        <Button type="submit" variant="outline">
          Filter
        </Button>
        <Button type="button" variant="outline" disabled={downloading} onClick={() => void download()}>
          Download CSV
        </Button>
      </form>
      {csvError && (
        <p className="text-sm text-destructive" role="alert" data-testid="audit-csv-error">
          {csvError}
        </p>
      )}
      {events.isLoading && <TableSkeleton rows={4} cols={4} label="Loading events" />}
      {events.isError && (
        <ErrorState error={events.error} title="Couldn't load the audit log" onRetry={() => void events.refetch()} />
      )}
      {events.data && rows.length === 0 && <p className="text-sm text-muted-foreground">No events match.</p>}
      {rows.length > 0 && (
        <AuditRows rows={rows} admin={scope === "admin"} showOrg={orgFilter} orgNames={orgNames} />
      )}
      {events.hasNextPage && (
        <div>
          <Button variant="outline" disabled={events.isFetchingNextPage} onClick={() => void events.fetchNextPage()}>
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}
