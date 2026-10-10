import { Link } from "@tanstack/react-router";
import { useInfiniteQuery } from "@tanstack/react-query";
import { portalKey, usageApi, type UsageCall, type UsageCallsPage, type UsageQuery } from "../../api";
import { formatDateTime, formatTokens, formatUsd } from "../../lib/format";
import { whatLabel, whereLabel } from "../../lib/usageLabels";
import { ResponsiveTable, type Column } from "../layout/ResponsiveTable";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { UsageError } from "./parts";

const COST_SOURCE: Record<string, string> = {
  provider: "reported by the provider",
  estimated: "estimated from the price table",
  unpriced: "no price on file: tokens only",
};

/**
 * LLM calls, newest or costliest first, with keyset paging on the server's `next`.
 * Shared by the table and by the page's filter chips (which read the first rows).
 */
export function useCallsQuery(scope: "org" | "me", query: UsageQuery) {
  return useInfiniteQuery<UsageCallsPage, Error, { pages: UsageCallsPage[] }, readonly unknown[], string | undefined>({
    queryKey: portalKey("usage", scope, "calls", query),
    queryFn: ({ pageParam }) => usageApi(scope).calls({ ...query, before: pageParam }),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next ?? undefined,
  });
}

/** The scan run and chat session a call belongs to, linked when the viewer may open them. */
function CallLinks({ call, viewerUid, openable }: { call: UsageCall; viewerUid?: string; openable: boolean }) {
  const slug = call.project_slug;
  const parts = [];
  if (call.scan_run_id !== null) {
    parts.push(
      slug && openable ? (
        <Link
          key="run"
          to="/p/$slug/scans/{-$runId}"
          params={{ slug, runId: String(call.scan_run_id) }}
          className="text-primary-text hover:underline"
        >
          Scan
        </Link>
      ) : (
        <span key="run">Scan</span>
      ),
    );
  }
  if (call.chat_session_id !== null) {
    // Chat is per user: only the viewer's own sessions are theirs to open.
    const own = !!viewerUid && call.user_uid === viewerUid;
    parts.push(
      slug && openable && own ? (
        <Link
          key="chat"
          to="/p/$slug/chat/{-$id}"
          params={{ slug, id: String(call.chat_session_id) }}
          className="text-primary-text hover:underline"
          data-testid={`call-chat-${call.id}`}
        >
          Chat
        </Link>
      ) : (
        <span key="chat" data-testid={`call-chat-${call.id}`}>
          {own ? "Chat" : "Chat (another member)"}
        </span>
      ),
    );
  }
  return <>{parts.flatMap((p, i) => (i === 0 ? [p] : [<span key={`sep${i}`}> · </span>, p]))}</>;
}

/**
 * LLM calls, newest or costliest first, with "Load more". `openable` lists the
 * project slugs the viewer can still open: a call of any other project (removed, or
 * no longer shared) links nowhere. Below `sm` each call stacks (PH-5).
 */
export function CallsTable({
  scope,
  query,
  viewerUid,
  openable,
  showWho = true,
}: {
  scope: "org" | "me";
  query: UsageQuery;
  viewerUid?: string;
  openable: Set<string>;
  /** Hide the Who column (a single member's view, and local mode, which has one user). */
  showWho?: boolean;
}) {
  const calls = useCallsQuery(scope, query);
  const rows = calls.data?.pages.flatMap((p) => p.items) ?? [];
  const columns: Column<UsageCall>[] = [
    {
      key: "when",
      header: "When",
      cell: (c) => <span className="whitespace-nowrap text-muted-foreground">{formatDateTime(c.created_at)}</span>,
    },
    {
      key: "project",
      header: "Project",
      cell: (c) => <span title={c.project_slug ?? undefined}>{c.project_name ?? c.project_slug ?? "-"}</span>,
    },
  ];
  if (showWho) {
    columns.push({
      key: "who",
      header: "Who",
      cell: (c) => (
        <>
          {c.actor_label ?? "-"}
          {c.client_name && <span className="block text-muted-foreground">{c.client_name}</span>}
        </>
      ),
    });
  }
  columns.push(
    {
      key: "what",
      header: "What",
      primary: true,
      cell: (c) => {
        const what = whatLabel(c.task);
        const where = whereLabel(c.source);
        return (
          <span className="whitespace-nowrap">
            {what}
            {where !== what && <span className="font-normal text-muted-foreground"> · {where}</span>}
          </span>
        );
      },
    },
    {
      key: "model",
      header: "Model",
      cell: (c) => (
        <span className="font-mono text-xs" title={`${c.provider} · key: ${c.key_scope}`}>
          {c.model_served ?? c.model_requested ?? c.provider}
        </span>
      ),
    },
    {
      key: "tokens",
      header: "Tokens in / out",
      align: "right",
      cell: (c) => (
        <span className="whitespace-nowrap">
          {formatTokens(c.input_tokens)} / {formatTokens(c.output_tokens)}
        </span>
      ),
    },
    {
      key: "cost",
      header: "Est. cost",
      align: "right",
      cell: (c) => (
        <span className="whitespace-nowrap" title={COST_SOURCE[c.cost_source]}>
          {c.cost_source === "unpriced" ? (
            <span className="text-warning">unpriced</span>
          ) : (
            <>
              {formatUsd(c.cost_usd)}
              {c.cost_source === "provider" && <span className="text-muted-foreground"> *</span>}
            </>
          )}
        </span>
      ),
    },
    {
      key: "details",
      header: "Details",
      cell: (c) => (
        <span className="text-muted-foreground">
          {c.subject && (
            <span className="block max-w-[16rem] truncate font-mono text-xs" title={c.subject}>
              {c.subject}
            </span>
          )}
          <CallLinks call={c} viewerUid={viewerUid} openable={!!c.project_slug && openable.has(c.project_slug)} />
        </span>
      ),
    },
  );
  return (
    <div className="flex flex-col gap-3" data-testid="calls-table">
      {calls.isLoading && <Skeleton className="h-24" />}
      {calls.isError && (
        <UsageError
          error={calls.error}
          what="these calls"
          title="Couldn't load these calls"
          onRetry={() => void calls.refetch()}
        />
      )}
      {calls.data && rows.length === 0 && <p className="text-sm text-muted-foreground">No calls match.</p>}
      {rows.length > 0 && (
        <>
          <ResponsiveTable columns={columns} rows={rows} rowKey={(c) => String(c.id)} rowTestId={(c) => `call-${c.id}`} />
          {rows.some((c) => c.cost_source === "provider") && (
            <p className="text-[11px] text-muted-foreground">* cost reported by the provider.</p>
          )}
        </>
      )}
      {calls.hasNextPage && (
        <div>
          <Button variant="outline" disabled={calls.isFetchingNextPage} onClick={() => void calls.fetchNextPage()}>
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}
