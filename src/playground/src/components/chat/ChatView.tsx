import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import { useExplorer } from "../../store";
import { SessionList } from "./SessionList";
import { MessageThread } from "./MessageThread";
import { ModelSelect } from "./ModelSelect";

/**
 * The Chat view: session sidebar plus thread column.
 *
 * Two panes rather than the Explorer's three — there is no detail panel to fill,
 * and the answers link into the Explorer for that.
 *
 * The header carries live provider/model dropdowns. Switching them PATCHes the
 * session and takes effect on the next turn; turns already in the transcript
 * keep their own recorded model, so switching never rewrites history.
 */
export function ChatView() {
  const activeSessionId = useExplorer((s) => s.activeSessionId);
  const queryClient = useQueryClient();

  // Header context for the open session (provider/model), and the reason a
  // deleted-elsewhere session degrades gracefully rather than 404-looping.
  const sessions = useQuery({
    queryKey: ["chat", "sessions"],
    queryFn: api.chatSessions,
  });
  const active = sessions.data?.find((s) => s.id === activeSessionId);

  const update = useMutation({
    mutationFn: (vars: { id: number; provider?: string; model?: string }) =>
      api.chatUpdateSession(vars.id, { provider: vars.provider, model: vars.model }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat", "sessions"] });
    },
  });

  return (
    <div className="flex min-h-0 flex-1">
      <aside className="w-72 shrink-0 border-r border-border bg-panel">
        <SessionList />
      </aside>
      <main className="flex min-w-0 flex-1 flex-col bg-bg">
        {activeSessionId === null ? (
          <div className="flex h-full items-center justify-center p-8 text-center text-sm text-muted">
            Select a chat, or start a new one.
          </div>
        ) : (
          <>
            {active && (
              <div className="flex items-center gap-3 border-b border-border px-4 py-2">
                <span className="min-w-0 flex-1 truncate text-sm font-medium text-fg">
                  {active.title}
                </span>
                <ModelSelect
                  compact
                  provider={active.provider}
                  model={active.model}
                  disabled={update.isPending}
                  onChange={(next) =>
                    update.mutate({
                      id: active.id,
                      // Send provider only when it actually changed, so the
                      // server doesn't reset the model on a model-only switch.
                      provider:
                        next.provider === active.provider ? undefined : next.provider,
                      model: next.model || undefined,
                    })
                  }
                />
              </div>
            )}
            {update.isError && (
              <div className="border-b border-border px-4 py-1 text-xs text-rose-400">
                {(update.error as Error).message}
              </div>
            )}
            <div className="min-h-0 flex-1">
              <MessageThread key={activeSessionId} sessionId={activeSessionId} />
            </div>
          </>
        )}
      </main>
    </div>
  );
}
