import { useState, type FormEvent } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { auditApi, type AuditEventRow, type AuditFilters } from "../../api";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Skeleton } from "../ui/skeleton";
import { Field } from "./Field";

type Page = { events: AuditEventRow[]; next: number | null };

function when(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleString();
}

/** The `fields` object as `key=value` pairs, for a one-line detail cell. */
function details(fields: Record<string, unknown>): string {
  return Object.entries(fields)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ");
}

/** Save a Blob under `name` through a temporary object URL (never a plain link: the API needs a header). */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/**
 * Security events, newest first, with event / actor / date filters and "Load more"
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
      setCsvError(err instanceof Error ? err.message : "The download failed");
    } finally {
      setDownloading(false);
    }
  };

  const rows = events.data?.pages.flatMap((p) => p.events) ?? [];
  return (
    <div className="flex flex-col gap-4" data-testid="audit-table">
      <form onSubmit={apply} className="flex flex-wrap items-end gap-3" data-testid="audit-filters">
        <Field label="Event" className="w-44">
          {(p) => (
            <Input {...p} placeholder="member_added" value={draft.event} onChange={(e) => setDraft({ ...draft, event: e.target.value })} />
          )}
        </Field>
        <Field label="Actor" className="w-40">
          {(p) => <Input {...p} placeholder="@ben" value={draft.actor} onChange={(e) => setDraft({ ...draft, actor: e.target.value })} />}
        </Field>
        <Field label="From" className="w-40">
          {(p) => <Input {...p} type="date" value={draft.from} onChange={(e) => setDraft({ ...draft, from: e.target.value })} />}
        </Field>
        <Field label="To" className="w-40">
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
      {events.isLoading && <Skeleton className="h-24" />}
      {events.isError && <p className="text-sm text-destructive">Failed to load: {events.error.message}</p>}
      {events.data && rows.length === 0 && <p className="text-sm text-muted-foreground">No events match.</p>}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1.5 pr-3 font-medium">When</th>
                <th className="py-1.5 pr-3 font-medium">Actor</th>
                <th className="py-1.5 pr-3 font-medium">Event</th>
                <th className="py-1.5 pr-3 font-medium">Target</th>
                <th className="py-1.5 font-medium">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((r) => (
                <tr key={r.id} data-testid={`audit-${r.id}`}>
                  <td className="whitespace-nowrap py-1.5 pr-3 text-muted-foreground">{when(r.created_at)}</td>
                  <td className="py-1.5 pr-3">{r.actor?.label ?? r.actor?.uid ?? "-"}</td>
                  <td className="py-1.5 pr-3 font-mono">{r.event}</td>
                  <td className="py-1.5 pr-3 font-mono">{r.target ?? ""}</td>
                  <td className="break-all py-1.5 font-mono text-muted-foreground">
                    {[r.org && scope === "admin" ? `org=${r.org}` : "", details(r.fields)].filter(Boolean).join(" ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
