import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { useExplorer } from "../../store";
import { SessionList } from "./SessionList";
import { MessageThread } from "./MessageThread";

/**
 * The Chat view: session sidebar plus thread column.
 *
 * Two panes rather than the Explorer's three — there is no detail panel to fill,
 * and the answers link into the Explorer for that.
 *
 * The header is just the title. The provider/model dropdowns sit above the
 * composer instead, next to the decision they affect — and MessageThread owns
 * them, because it is the only component that knows whether a turn is streaming
 * (switching mid-stream would change the session row under the in-flight turn).
 */
export function ChatView() {
  const activeSessionId = useExplorer((s) => s.activeSessionId);

  // Header context for the open session, and the reason a deleted-elsewhere
  // session degrades gracefully rather than 404-looping.
  const sessions = useQuery({
    queryKey: ["chat", "sessions"],
    queryFn: api.chatSessions,
  });
  const active = sessions.data?.find((s) => s.id === activeSessionId);

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
              </div>
            )}
            <div className="min-h-0 flex-1">
              <MessageThread
                key={activeSessionId}
                sessionId={activeSessionId}
                session={active}
              />
            </div>
          </>
        )}
      </main>
    </div>
  );
}
