import { useState } from "react";
import { useChatSessions } from "../../lib/chatSessions";
import { useActiveSessionId } from "../../lib/nav";
import { useSlug } from "../../lib/project";
import { PageContainer } from "../layout/PageContainer";
import { MessageThread } from "./MessageThread";

/** Which thread is mounted: a new one per session switch, the same one when it adopts its own new session. */
interface ThreadSlot {
  /** The route's session id the slot was last rendered for. */
  id: number | null;
  /** Bumped to remount the thread (a different session, or a new chat). */
  token: number;
  /** The session the mounted thread created on its first send, if any. */
  adopted: number | null;
}

/**
 * The Chat view: one column, the thread at full width (M2f-3 §4.6). The session
 * list lives in the sidebar's Chats section.
 *
 * The header is the session's title (or "New chat"). The provider/model dropdowns
 * sit above the composer instead, next to the decision they affect - and
 * MessageThread owns them, because it is the only component that knows whether a
 * turn is streaming (switching mid-stream would change the session row under the
 * in-flight turn).
 *
 * `/chat` is a draft: the thread creates its session on the first send and the URL
 * moves to `/chat/<id>`. The thread keeps its React key across that move (it
 * adopted the id), so the reply that is streaming is never aborted; any other
 * change of session remounts it.
 */
export function ChatView() {
  const slug = useSlug();
  const activeSessionId = useActiveSessionId();
  const sessions = useChatSessions(slug);
  const active = activeSessionId === null ? undefined : sessions.data?.find((s) => s.id === activeSessionId);

  const [slot, setSlot] = useState<ThreadSlot>({ id: activeSessionId, token: 0, adopted: null });
  if (slot.id !== activeSessionId) {
    const keep = activeSessionId !== null && activeSessionId === slot.adopted;
    setSlot({ id: activeSessionId, token: keep ? slot.token : slot.token + 1, adopted: keep ? slot.adopted : null });
  }

  const title = activeSessionId === null ? "New chat" : (active?.title ?? "Chat");

  return (
    <PageContainer width="full" className="flex min-h-0 flex-1 flex-col p-0 sm:p-0">
      <h1 className="sr-only">{title}</h1>
      <div className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground" data-testid="chat-title">
          {title}
        </span>
      </div>
      <div className="min-h-0 flex-1">
        <MessageThread
          key={`${slug}:${slot.token}`}
          sessionId={activeSessionId}
          session={active}
          onCreated={(id) => setSlot((s) => ({ ...s, adopted: id }))}
        />
      </div>
    </PageContainer>
  );
}
