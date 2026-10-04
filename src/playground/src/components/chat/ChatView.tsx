import { useReadOnly } from "../../lib/identity";
import { useActiveSessionId } from "../../lib/nav";
import { useProjectQuery, useSlug } from "../../lib/project";
import { SessionList } from "./SessionList";
import { MessageThread } from "./MessageThread";
import { Empty, EmptyDescription } from "../ui/empty";

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
  const slug = useSlug();
  const activeSessionId = useActiveSessionId();
  const readOnly = useReadOnly();

  // Header context for the open session, and the reason a deleted-elsewhere
  // session degrades gracefully rather than 404-looping.
  const sessions = useProjectQuery(["chat", "sessions"], (api) => api.chatSessions());
  const active = sessions.data?.find((s) => s.id === activeSessionId);

  if (readOnly) {
    return (
      <Empty className="h-full p-8" data-testid="chat-read-only">
        <EmptyDescription>Chat is not available while viewing as an instance administrator (read-only).</EmptyDescription>
      </Empty>
    );
  }

  return (
    <div className="flex min-h-0 flex-1">
      <aside className="w-72 shrink-0 border-r border-border bg-sidebar">
        <SessionList />
      </aside>
      <main className="flex min-w-0 flex-1 flex-col bg-background">
        {activeSessionId === null ? (
          <Empty className="h-full p-8">
            <EmptyDescription>Select a chat, or start a new one.</EmptyDescription>
          </Empty>
        ) : (
          <>
            {active && (
              <div className="flex items-center gap-3 border-b border-border px-4 py-2">
                <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
                  {active.title}
                </span>
              </div>
            )}
            <div className="min-h-0 flex-1">
              <MessageThread
                key={`${slug}:${activeSessionId}`}
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
