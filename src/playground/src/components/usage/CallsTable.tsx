import { Link } from "@tanstack/react-router";
import { useInfiniteQuery } from "@tanstack/react-query";
import { portalKey, usageApi, type UsageCall, type UsageCallsPage, type UsageQuery } from "../../api";
import { authMessage } from "../../lib/authErrors";
import { formatTokens, formatUsd } from "../../lib/format";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";

function when(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleString();
}

const COST_SOURCE: Record<string, string> = {
  provider: "reported by the provider",
  estimated: "estimated from the price table",
  unpriced: "no price on file: tokens only",
};

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
          Run #{call.scan_run_id}
        </Link>
      ) : (
        <span key="run">Run #{call.scan_run_id}</span>
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
          Chat #{call.chat_session_id}
        </Link>
      ) : (
        <span key="chat" data-testid={`call-chat-${call.id}`}>
          #{call.chat_session_id}
        </span>
      ),
    );
  }
  return <>{parts.flatMap((p, i) => (i === 0 ? [p] : [<span key={`sep${i}`}> · </span>, p]))}</>;
}

/**
 * LLM calls, newest or costliest first, with "Load more" (keyset paging on the
 * server's `next`). `openable` lists the project slugs the viewer can still open:
 * a call of any other project (removed, or no longer shared) links nowhere.
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
  /** Hide the Who column (a single member's view). */
  showWho?: boolean;
}) {
  const calls = useInfiniteQuery<UsageCallsPage, Error, { pages: UsageCallsPage[] }, readonly unknown[], string | undefined>({
    queryKey: portalKey("usage", scope, "calls", query),
    queryFn: ({ pageParam }) => usageApi(scope).calls({ ...query, before: pageParam }),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next ?? undefined,
  });
  const rows = calls.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="flex flex-col gap-3" data-testid="calls-table">
      {calls.isLoading && <Skeleton className="h-24" />}
      {calls.isError && <p className="text-sm text-destructive">Failed to load: {authMessage(calls.error)}</p>}
      {calls.data && rows.length === 0 && <p className="text-sm text-muted-foreground">No calls match.</p>}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1.5 pr-3 font-medium">When</th>
                <th className="py-1.5 pr-3 font-medium">Project</th>
                {showWho && <th className="py-1.5 pr-3 font-medium">Who</th>}
                <th className="py-1.5 pr-3 font-medium">What</th>
                <th className="py-1.5 pr-3 font-medium">Model</th>
                <th className="py-1.5 pr-3 text-right font-medium">Tokens in / out</th>
                <th className="py-1.5 pr-3 text-right font-medium">Est. cost</th>
                <th className="py-1.5 font-medium">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((c) => (
                <tr key={c.id} data-testid={`call-${c.id}`}>
                  <td className="whitespace-nowrap py-1.5 pr-3 text-muted-foreground">{when(c.created_at)}</td>
                  <td className="max-w-[10rem] truncate py-1.5 pr-3" title={c.project_slug ?? undefined}>
                    {c.project_name ?? c.project_slug ?? "-"}
                  </td>
                  {showWho && (
                    <td className="max-w-[10rem] truncate py-1.5 pr-3">
                      {c.actor_label ?? "-"}
                      {c.client_name && <span className="block text-muted-foreground">{c.client_name}</span>}
                    </td>
                  )}
                  <td className="whitespace-nowrap py-1.5 pr-3">
                    {c.task}
                    <span className="text-muted-foreground"> · {c.source}</span>
                  </td>
                  <td className="max-w-[14rem] truncate py-1.5 pr-3 font-mono" title={`${c.provider} · key: ${c.key_scope}`}>
                    {c.model_served ?? c.model_requested ?? c.provider}
                  </td>
                  <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums">
                    {formatTokens(c.input_tokens)} / {formatTokens(c.output_tokens)}
                  </td>
                  <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums" title={COST_SOURCE[c.cost_source]}>
                    {c.cost_source === "unpriced" ? (
                      <span className="text-warning">unpriced</span>
                    ) : (
                      <>
                        {formatUsd(c.cost_usd)}
                        {c.cost_source === "provider" && <span className="text-muted-foreground"> *</span>}
                      </>
                    )}
                  </td>
                  <td className="py-1.5 text-muted-foreground">
                    {c.subject && (
                      <span className="block max-w-[16rem] truncate font-mono" title={c.subject}>
                        {c.subject}
                      </span>
                    )}
                    <CallLinks call={c} viewerUid={viewerUid} openable={!!c.project_slug && openable.has(c.project_slug)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.some((c) => c.cost_source === "provider") && (
            <p className="mt-2 text-[11px] text-muted-foreground">* cost reported by the provider.</p>
          )}
        </div>
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
