import { errorMessage } from "../../lib/apiErrors";
import { useState, type FormEvent } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { auditApi, type AuditEventRow, type AuditFilters } from "../../api";
import { AUDIT_GROUPS, auditDetails, auditEventsIn, auditLabel } from "../../lib/auditEvents";
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
 * Security events, newest first, with a grouped event select and actor / date filters and "Load more"
 * (keyset paging on the server's `next`). `scope` picks the endpoint: the org's log
 * (with a CSV download) or the instance admin's log of events that belong to no org.
 */
export function AuditTable({ scope, filename = "whygraph-audit.csv" }: { scope: "org" | "admin"; filename?: string }) {
  const [draft, setDraft] = useState({ event: "", actor: "", from: "", to: "" });
  const [applied, setApplied] = useState<AuditFilters>({});
  const [csvError, setCsvError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  const fetchPage = scope === "org" ? auditApi.org : auditApi.admin;
  const events = useInfiniteQuery<Page, Error, { pages: Page[] }, readonly unknown[], number | undefined>({
    queryKey: ["@audit", scope, applied],
    queryFn: ({ pageParam }) => fetchPage({ ...applied, before: pageParam }),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next ?? undefined,
  });

  const apply = (e: FormEvent) => {
    e.preventDefault();
    setApplied(Object.fromEntries(Object.entries(draft).filter(([, v]) => v.trim() !== "")) as AuditFilters);
  };
  const download = async () => {
    setCsvError(null);
    setDownloading(true);
    try {
      saveBlob(await auditApi.csv(applied), filename);
    } catch (err) {
      setCsvError(errorMessage(err));
    } finally {
      setDownloading(false);
    }
  };

  const rows = events.data?.pages.flatMap((p) => p.events) ?? [];
  const columns: Column<AuditEventRow>[] = [
    {
      key: "event",
      header: "Event",
      primary: true,
      cell: (r) => <span title={r.event}>{auditLabel(r.event)}</span>,
    },
    {
      key: "when",
      header: "When",
      cell: (r) => <span className="whitespace-nowrap text-muted-foreground">{formatDateTime(r.created_at)}</span>,
    },
    { key: "actor", header: "Actor", cell: (r) => r.actor?.label ?? r.actor?.uid ?? "System" },
    { key: "target", header: "Target", cell: (r) => r.target_label ?? r.target ?? "" },
    {
      key: "details",
      header: "Details",
      cell: (r) => {
        const pairs = auditDetails(r, scope === "admin");
        if (pairs.length === 0) return null;
        return (
          <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-xs">
            {pairs.map((d) => (
              <div key={d.label} className="contents">
                <dt className="text-muted-foreground">{d.label}</dt>
                <dd className="min-w-0 break-words">{d.value}</dd>
              </div>
            ))}
          </dl>
        );
      },
    },
  ];
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
        {scope === "org" && (
          <Button type="button" variant="outline" disabled={downloading} onClick={() => void download()}>
            Download CSV
          </Button>
        )}
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
        <ResponsiveTable columns={columns} rows={rows} rowKey={(r) => String(r.id)} rowTestId={(r) => `audit-${r.id}`} />
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
