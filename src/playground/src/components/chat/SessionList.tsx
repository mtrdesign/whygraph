import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import { api } from "../../api";
import { useExplorer } from "../../store";
import { Button, EmptyState, IconButton, Input, Spinner } from "../../lib/ui";
import { ProviderPicker } from "./ProviderPicker";

/**
 * The session sidebar: new chat, select, rename, delete.
 *
 * Mirrors the Explorer's tree aside (same width, same panel surface) so the two
 * views feel like one app rather than two bolted together.
 */
export function SessionList() {
  const queryClient = useQueryClient();
  const activeSessionId = useExplorer((s) => s.activeSessionId);
  const setActiveSession = useExplorer((s) => s.setActiveSession);

  const [picking, setPicking] = useState(false);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [draftTitle, setDraftTitle] = useState("");

  const sessions = useQuery({
    queryKey: ["chat", "sessions"],
    queryFn: api.chatSessions,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["chat", "sessions"] });

  const create = useMutation({
    mutationFn: (vars: { provider: string; model: string }) =>
      api.chatCreateSession(vars),
    onSuccess: (session) => {
      setPicking(false);
      setActiveSession(session.id);
      invalidate();
    },
  });

  const rename = useMutation({
    mutationFn: (vars: { id: number; title: string }) =>
      api.chatUpdateSession(vars.id, { title: vars.title }),
    onSuccess: () => {
      setRenamingId(null);
      invalidate();
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.chatDeleteSession(id),
    onSuccess: (_data, id) => {
      if (activeSessionId === id) setActiveSession(null);
      invalidate();
    },
  });

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border p-3">
        <Button onClick={() => setPicking((v) => !v)} className="w-full">
          {picking ? "Close" : "+ New chat"}
        </Button>
      </div>

      {picking && (
        <ProviderPicker
          creating={create.isPending}
          onCancel={() => setPicking(false)}
          onCreate={(provider, model) => create.mutate({ provider, model })}
        />
      )}

      {create.isError && (
        <div className="px-3 py-2 text-xs text-rose-400">
          {(create.error as Error).message}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        {sessions.isLoading && (
          <div className="p-3">
            <Spinner label="Loading sessions…" />
          </div>
        )}
        {sessions.isError && (
          <div className="p-3 text-xs text-rose-400">
            {(sessions.error as Error).message}
          </div>
        )}
        {sessions.data?.length === 0 && (
          <EmptyState>No chats yet. Start one above.</EmptyState>
        )}

        {sessions.data?.map((session) => (
          <div
            key={session.id}
            className={clsx(
              "group border-b border-border/60 px-3 py-2",
              session.id === activeSessionId ? "bg-panel2" : "hover:bg-panel2/50",
            )}
          >
            {renamingId === session.id ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  const title = draftTitle.trim();
                  if (title) rename.mutate({ id: session.id, title });
                  else setRenamingId(null);
                }}
              >
                <Input
                  autoFocus
                  value={draftTitle}
                  onChange={(e) => setDraftTitle(e.target.value)}
                  onBlur={() => setRenamingId(null)}
                  className="px-2 py-1 text-xs"
                />
              </form>
            ) : (
              <>
                <div className="flex items-start gap-1">
                  <button
                    type="button"
                    onClick={() => setActiveSession(session.id)}
                    className="min-w-0 flex-1 text-left"
                  >
                    <div
                      className={clsx(
                        "truncate text-sm",
                        session.id === activeSessionId ? "text-fg" : "text-muted",
                      )}
                    >
                      {session.title}
                    </div>
                  </button>
                  {/* Actions stay hidden until hover so the list reads as titles. */}
                  <div className="flex opacity-0 transition-opacity group-hover:opacity-100">
                    <IconButton
                      label="Rename"
                      onClick={() => {
                        setRenamingId(session.id);
                        setDraftTitle(session.title);
                      }}
                    >
                      ✎
                    </IconButton>
                    <IconButton
                      label="Delete"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (confirm(`Delete "${session.title}"?`)) {
                          remove.mutate(session.id);
                        }
                      }}
                    >
                      ✕
                    </IconButton>
                  </div>
                </div>
                <div className="truncate text-[10px] text-muted">
                  {session.provider} · {session.model}
                  {session.message_count ? ` · ${session.message_count} msgs` : ""}
                </div>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
