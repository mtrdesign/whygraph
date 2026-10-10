import { useEffect, useId, useRef, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { MoreHorizontalIcon, PlusIcon } from "lucide-react";
import type { ChatSession } from "../../api";
import { useChatSessions, useDeleteSession, useRenameSession, useStartChat } from "../../lib/chatSessions";
import { errorMessage } from "../../lib/apiErrors";
import { formatDateTime, formatRelative } from "../../lib/format";
import { useActiveSessionId } from "../../lib/nav";
import { plural } from "../../lib/plural";
import { useUi } from "../../store";
import { ConfirmDialog } from "../portal/ConfirmDialog";
import { DisabledReason } from "../state/DisabledReason";
import { ErrorState } from "../state/ErrorState";
import { Button } from "../ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";
import { Input } from "../ui/input";
import { Skeleton } from "../ui/skeleton";
import { Spinner } from "../ui/spinner";
import { cn } from "@/lib/utils";

/** How many sessions show before "Show more", and how many each click adds (§0.3 #26). */
export const CHATS_PAGE = 20;

/** The inline rename field: Enter saves, Escape cancels, an empty title is refused in place. */
function RenameField({
  session,
  pending,
  error,
  onSave,
  onCancel,
}: {
  session: ChatSession;
  pending: boolean;
  error: string | null;
  onSave: (title: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(session.title);
  const [empty, setEmpty] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  // The menu that opened the field hands focus back on close; take it after that.
  useEffect(() => {
    const t = setTimeout(() => {
      ref.current?.focus();
      ref.current?.select();
    }, 0);
    return () => clearTimeout(t);
  }, []);
  const message = empty ? "Enter a title." : error;
  return (
    <form
      className="flex flex-col gap-1 px-2 py-1"
      onSubmit={(e) => {
        e.preventDefault();
        const title = value.trim();
        if (!title) {
          setEmpty(true);
          return;
        }
        if (title === session.title) onCancel();
        else onSave(title);
      }}
    >
      <Input
        ref={ref}
        aria-label="Chat title"
        value={value}
        disabled={pending}
        aria-invalid={message ? true : undefined}
        onChange={(e) => {
          setValue(e.target.value);
          setEmpty(false);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            onCancel();
          }
        }}
        className="h-7 px-2 text-xs"
      />
      {message && (
        <p className="text-xs text-destructive" role="alert">
          {message}
        </p>
      )}
    </form>
  );
}

function SessionRow({
  slug,
  session,
  active,
  streaming,
  onRename,
  onDelete,
}: {
  slug: string;
  session: ChatSession;
  active: boolean;
  streaming: boolean;
  onRename: () => void;
  onDelete: () => void;
}) {
  const setNavOpen = useUi((s) => s.setNavOpen);
  // Rename is chosen inside the menu; the field takes focus once the menu has closed.
  const renaming = useRef(false);
  const count = session.message_count;
  return (
    <li
      className={cn(
        "group/row relative flex items-center rounded-md",
        active ? "bg-primary-soft text-primary-text" : "hover:bg-accent",
      )}
      data-testid="chat-row"
    >
      <Link
        to="/p/$slug/chat/{-$id}"
        params={{ slug, id: String(session.id) }}
        aria-current={active ? "page" : undefined}
        title={count !== undefined ? `${session.title} (${plural(count, "message")})` : session.title}
        onClick={() => setNavOpen(false)}
        className="flex min-w-0 flex-1 flex-col rounded-md px-2 py-1.5 outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className={cn("truncate text-[13px]", active ? "font-medium" : "text-foreground")}>{session.title}</span>
        <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
          {streaming && <Spinner className="size-3" aria-label="Replying" data-testid="chat-row-streaming" />}
          <time dateTime={session.updated_at} title={formatDateTime(session.updated_at)}>
            {formatRelative(session.updated_at)}
          </time>
        </span>
      </Link>
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label={`Actions for ${session.title}`}
          className="mr-1 flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-background hover:text-foreground focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring data-[popup-open]:opacity-100 md:opacity-0 md:group-hover/row:opacity-100 md:group-focus-within/row:opacity-100"
        >
          <MoreHorizontalIcon className="size-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          className="w-36"
          finalFocus={() => !renaming.current}
        >
          <DropdownMenuItem
            onClick={() => {
              renaming.current = true;
              onRename();
            }}
          >
            Rename
          </DropdownMenuItem>
          <DropdownMenuItem variant="destructive" onClick={onDelete}>
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </li>
  );
}

/**
 * The sidebar's Chats section (M2f-3 §4.6): a "+" that opens a new chat, then the
 * caller's sessions (20, then "Show more"), each a link with a "..." menu to rename
 * or delete it. The sidebar renders it only for a project the caller may chat on
 * (initialized, not linked), so a viewer never sends the sessions request.
 */
export function ChatsSection({ slug, budgetStopped }: { slug: string; budgetStopped: boolean }) {
  const navigate = useNavigate();
  const sessions = useChatSessions(slug);
  const rename = useRenameSession(slug);
  const remove = useDeleteSession(slug);
  const startChat = useStartChat(slug);
  const activeId = useActiveSessionId();
  const streamingId = useUi((s) => s.streamingSessionId);
  const [shown, setShown] = useState(CHATS_PAGE);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<ChatSession | null>(null);
  // The sidebar renders twice (desktop and the phone sheet), so the heading id is per instance.
  const headingId = useId();

  const plus = (
    <Button
      variant="ghost"
      size="icon-xs"
      aria-label="New chat"
      disabled={budgetStopped}
      onClick={startChat}
      className="text-muted-foreground hover:text-foreground"
    >
      <PlusIcon />
    </Button>
  );

  const list = sessions.data ?? [];
  const visible = list.slice(0, shown);

  return (
    // The sidebar's only scroller. It keeps four rows' height (or all of a shorter list)
    // before giving way, so on a short window the whole sidebar scrolls instead (§4.6).
    <section
      aria-labelledby={headingId}
      className={cn(
        "flex flex-1 flex-col gap-0.5 overflow-y-auto px-3 pb-2",
        visible.length >= 4 ? "min-h-48" : "min-h-fit",
      )}
      data-testid="chats-section"
    >
      <div className="flex items-center justify-between px-2 pb-1 pt-2">
        <h2 id={headingId} className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Chats
        </h2>
        {budgetStopped ? <DisabledReason reason="Monthly budget reached">{plus}</DisabledReason> : plus}
      </div>

      {sessions.isPending && (
        <div className="flex flex-col gap-1.5 px-2 py-1" aria-busy="true" role="status" data-testid="skeleton">
          <span className="sr-only">Loading chats</span>
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-8 w-full" />
          ))}
        </div>
      )}
      {sessions.isError && (
        <ErrorState
          size="inline"
          error={sessions.error}
          context="chat"
          onRetry={() => void sessions.refetch()}
          className="px-2 py-1 text-xs"
        />
      )}
      {sessions.isSuccess && list.length === 0 && (
        <p className="px-2 py-1 text-xs text-muted-foreground">No chats yet</p>
      )}

      {visible.length > 0 && (
        <ul className="flex flex-col gap-0.5">
          {visible.map((session) =>
            renamingId === session.id ? (
              <li key={session.id}>
                <RenameField
                  session={session}
                  pending={rename.isPending}
                  error={rename.isError ? errorMessage(rename.error) : null}
                  onCancel={() => {
                    rename.reset();
                    setRenamingId(null);
                  }}
                  onSave={(title) =>
                    rename.mutate(
                      { id: session.id, title },
                      {
                        onSuccess: () => setRenamingId(null),
                      },
                    )
                  }
                />
              </li>
            ) : (
              <SessionRow
                key={session.id}
                slug={slug}
                session={session}
                active={session.id === activeId}
                streaming={session.id === streamingId}
                onRename={() => {
                  rename.reset();
                  setRenamingId(session.id);
                }}
                onDelete={() => {
                  remove.reset();
                  setDeleting(session);
                }}
              />
            ),
          )}
        </ul>
      )}
      {list.length > shown && (
        <button
          type="button"
          onClick={() => setShown((n) => n + CHATS_PAGE)}
          className="mx-2 mt-1 self-start text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          Show more
        </button>
      )}

      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title="Delete chat"
        description={deleting ? `Delete "${deleting.title}"? This removes its messages for good.` : ""}
        confirmLabel="Delete"
        pending={remove.isPending}
        error={remove.isError ? errorMessage(remove.error) : null}
        onConfirm={() => {
          const target = deleting;
          if (!target) return;
          remove.mutate(target.id, {
            onSuccess: () => {
              setDeleting(null);
              if (target.id === activeId) {
                void navigate({ to: "/p/$slug/chat/{-$id}", params: { slug, id: undefined } });
              }
            },
          });
        }}
      />
    </section>
  );
}
