import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PencilIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { useActiveSessionId, useSetActiveSession } from "../../lib/nav";
import { useProjectApi, useProjectKey, useProjectQuery } from "../../lib/project";
import { Loading } from "../Loading";
import { Button } from "../ui/button";
import { Empty, EmptyDescription } from "../ui/empty";
import { Input } from "../ui/input";
import { ScrollArea } from "../ui/scroll-area";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";
import { cn } from "@/lib/utils";

/** A ghost icon button with a tooltip - the row actions in the session list. */
function RowAction({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={label}
            disabled={disabled}
            onClick={onClick}
            className="text-muted-foreground"
          />
        }
      >
        {children}
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

/**
 * The session sidebar: new chat, select, rename, delete.
 *
 * Mirrors the Explorer's tree aside (same width, same panel surface) so the two
 * views feel like one app rather than two bolted together.
 *
 * "+ New chat" creates immediately — no provider step. The server already
 * resolves every field from config, and the dropdowns above the composer let the
 * choice be changed afterwards, so making the user answer first was friction
 * with nothing behind it.
 */
export function SessionList() {
  const queryClient = useQueryClient();
  const api = useProjectApi();
  const key = useProjectKey();
  const activeSessionId = useActiveSessionId();
  const setActiveSession = useSetActiveSession();

  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [draftTitle, setDraftTitle] = useState("");

  const sessions = useProjectQuery(["chat", "sessions"], (api) => api.chatSessions());

  // Already in cache whenever a ModelSelect has rendered; used only to prefer a
  // *configured* provider over the config default.
  const providers = useProjectQuery(["chat", "providers"], (api) => api.chatProviders());

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: key("chat", "sessions") });

  const create = useMutation({
    mutationFn: () => {
      // No model: the server resolves it (Config.model_for("chat")).
      // No provider either if the list hasn't arrived — the server falls back
      // to [chat].provider rather than making the click wait on a fetch.
      const first =
        providers.data?.find((p) => p.configured) ?? providers.data?.[0];
      return api.chatCreateSession(first ? { provider: first.provider } : {});
    },
    onSuccess: (session) => {
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
        <Button
          onClick={() => create.mutate()}
          disabled={create.isPending}
          className="w-full"
        >
          <PlusIcon data-icon="inline-start" />
          {create.isPending ? "Creating…" : "New chat"}
        </Button>
      </div>

      {create.isError && (
        <div className="px-3 py-2 text-xs text-destructive">
          {(create.error as Error).message}
        </div>
      )}

      <ScrollArea className="min-h-0 flex-1">
        {sessions.isLoading && (
          <div className="p-3">
            <Loading label="Loading sessions…" />
          </div>
        )}
        {sessions.isError && (
          <div className="p-3 text-xs text-destructive">
            {(sessions.error as Error).message}
          </div>
        )}
        {sessions.data?.length === 0 && (
          <Empty className="p-4">
            <EmptyDescription>No chats yet. Start one above.</EmptyDescription>
          </Empty>
        )}

        {sessions.data?.map((session) => (
          <div
            key={session.id}
            className={cn(
              "group border-b border-border/60 px-3 py-2",
              session.id === activeSessionId ? "bg-accent" : "hover:bg-accent/50",
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
                  className="h-7 px-2 text-xs"
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
                      className={cn(
                        "truncate text-sm",
                        session.id === activeSessionId ? "text-foreground" : "text-muted-foreground",
                      )}
                    >
                      {session.title}
                    </div>
                  </button>
                  {/* Actions stay hidden until hover so the list reads as titles. */}
                  <div className="flex opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                    <RowAction
                      label="Rename"
                      onClick={() => {
                        setRenamingId(session.id);
                        setDraftTitle(session.title);
                      }}
                    >
                      <PencilIcon />
                    </RowAction>
                    <RowAction
                      label="Delete"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (confirm(`Delete "${session.title}"?`)) {
                          remove.mutate(session.id);
                        }
                      }}
                    >
                      <Trash2Icon />
                    </RowAction>
                  </div>
                </div>
                <div className="truncate text-[10px] text-muted-foreground">
                  {session.provider} · {session.model}
                  {session.message_count ? ` · ${session.message_count} msgs` : ""}
                </div>
              </>
            )}
          </div>
        ))}
      </ScrollArea>
    </div>
  );
}
