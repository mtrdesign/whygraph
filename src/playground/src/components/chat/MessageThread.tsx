import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, streamChat, type ChatSession } from "../../api";
import { EmptyState, Spinner } from "../../lib/ui";
import { MessageBubble, type AssistantTurn, type Turn } from "./MessageBubble";
import { Composer } from "./Composer";
import { ModelSelect } from "./ModelSelect";
import {
  applyTextDelta,
  applyToolCall,
  applyToolResult,
  emptyAssistantTurn,
  settleActivities,
  turnsFromMessages,
} from "./turns";

/**
 * The thread column: transcript, live streaming, and the composer.
 *
 * Two sources of truth, deliberately: the persisted transcript (fetched by
 * TanStack Query) and the in-flight turn (local state, fed by SSE). They are
 * concatenated for display, and on completion the transcript is refetched and
 * the local turn dropped — so what the user watched and what a reload shows are
 * the same thing, without optimistically writing rows the server owns.
 *
 * The provider/model dropdowns live here rather than in the view header because
 * only this component knows whether a turn is in flight — switching models
 * mid-stream would repoint the session row under the running turn.
 */
export function MessageThread({
  sessionId,
  session,
}: {
  sessionId: number;
  /** The session row, for the composer dropdowns. Absent while the list loads. */
  session?: ChatSession;
}) {
  const queryClient = useQueryClient();
  const [liveTurns, setLiveTurns] = useState<Turn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const transcript = useQuery({
    queryKey: ["chat", "transcript", sessionId],
    queryFn: () => api.chatTranscript(sessionId),
  });

  const update = useMutation({
    mutationFn: (vars: { provider?: string; model?: string }) =>
      api.chatUpdateSession(sessionId, vars),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat", "sessions"] });
    },
  });

  // Switching sessions must not carry another session's in-flight turn across.
  useEffect(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setLiveTurns([]);
    setStreaming(false);
  }, [sessionId]);

  // Abort an in-flight stream if the view unmounts mid-turn.
  useEffect(() => () => abortRef.current?.abort(), []);

  const persisted = transcript.data ? turnsFromMessages(transcript.data.messages) : [];
  const turns = [...persisted, ...liveTurns];

  // Stick to the bottom as content grows. `turns.length` alone isn't enough —
  // text deltas mutate the last turn in place — so the streaming flag and the
  // last turn's size are part of the dependency.
  const lastSize = JSON.stringify(turns[turns.length - 1] ?? "").length;
  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [turns.length, lastSize]);

  /** Mutate the in-flight assistant turn (always the last local turn). */
  const updateLive = useCallback((fn: (turn: AssistantTurn) => AssistantTurn) => {
    setLiveTurns((current) => {
      const next = [...current];
      const last = next[next.length - 1];
      if (last?.kind !== "assistant") return current;
      next[next.length - 1] = fn(last);
      return next;
    });
  }, []);

  const send = useCallback(
    async (content: string) => {
      // Started from the click/Enter handler, never an effect — StrictMode
      // double-invokes effects in dev, which would send the turn twice.
      const controller = new AbortController();
      abortRef.current = controller;
      setStreaming(true);
      setLiveTurns([
        { kind: "user", content },
        emptyAssistantTurn(),
      ]);

      try {
        await streamChat(
          sessionId,
          content,
          (event) => {
            switch (event.type) {
              case "text_delta":
                updateLive((t) => applyTextDelta(t, event.text));
                break;
              case "tool_call":
                updateLive((t) =>
                  applyToolCall(t, {
                    id: event.id,
                    name: event.name,
                    arguments: event.arguments,
                  }),
                );
                break;
              case "tool_result":
                updateLive((t) => applyToolResult(t, event.id, event.result));
                break;
              case "round_limit":
                updateLive((t) => ({ ...t, roundLimit: event.rounds }));
                break;
              case "error":
                // In-band and terminal: HTTP status was committed before the
                // first token, so a provider failure can only arrive this way.
                updateLive((t) => ({ ...settleActivities(t), error: event.message }));
                break;
              case "done":
                updateLive((t) => ({
                  ...settleActivities(t),
                  usage: {
                    input: event.input_tokens,
                    output: event.output_tokens,
                  },
                }));
                break;
            }
          },
          controller.signal,
        );
      } catch (err) {
        if ((err as Error).name === "AbortError") {
          updateLive((t) => ({ ...settleActivities(t), error: "Stopped." }));
        } else {
          updateLive((t) => ({
            ...settleActivities(t),
            error: (err as Error).message,
          }));
        }
      } finally {
        setStreaming(false);
        abortRef.current = null;
        // Refetch so the persisted rows replace the local turn — the server
        // persisted as it went, including on abort, so this is authoritative.
        const fresh = await queryClient
          .fetchQuery({
            queryKey: ["chat", "transcript", sessionId],
            queryFn: () => api.chatTranscript(sessionId),
            // The turn just wrote rows. Without this the app-wide 30s
            // staleTime makes fetchQuery resolve from cache with the *pre-send*
            // transcript, and clearing liveTurns below then erases the reply
            // the user just watched stream in.
            staleTime: 0,
          })
          .catch(() => null);
        if (fresh) {
          setLiveTurns([]);
        } else {
          // Keep the streamed copy — it's the only record on screen — but say
          // so, and settle it: a stream that died without a terminal frame
          // never ran settleActivities from the event switch.
          updateLive((t) => ({
            ...settleActivities(t),
            error:
              t.error ?? "Couldn't refresh the transcript — showing the streamed copy.",
          }));
        }
        // The sidebar shows titles and message counts, both of which just moved.
        queryClient.invalidateQueries({ queryKey: ["chat", "sessions"] });
      }
    },
    [queryClient, sessionId, updateLive],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-auto p-4">
        {transcript.isLoading && <Spinner label="Loading transcript…" />}
        {transcript.isError && (
          <div className="text-sm text-rose-400">
            {(transcript.error as Error).message}
          </div>
        )}
        {!transcript.isLoading && turns.length === 0 && (
          <EmptyState>
            Ask why a module is shaped the way it is, what changed around an area
            recently, or for a walk through a symbol's callers. The assistant reads
            CodeGraph, the WhyGraph history, and the source to answer.
          </EmptyState>
        )}
        {/* Persisted turns key on their first row's id; the two live turns key
            on their kind (there is only ever one of each). Index keys would
            reassign identity across the live→persisted swap. */}
        {turns.map((turn) => (
          <MessageBubble
            key={turn.id !== undefined ? `row-${turn.id}` : `live-${turn.kind}`}
            turn={turn}
          />
        ))}
      </div>

      {session && (
        <div className="border-t border-border px-4 py-2">
          <ModelSelect
            compact
            provider={session.provider}
            model={session.model}
            // Streaming is part of this: repointing the session mid-turn would
            // change the row the in-flight turn is attributed to.
            disabled={update.isPending || streaming}
            onChange={(next) =>
              update.mutate({
                // Send provider only when it actually changed, so the server
                // doesn't reset the model on a model-only switch.
                provider:
                  next.provider === session.provider ? undefined : next.provider,
                model: next.model || undefined,
              })
            }
          />
          {update.isError && (
            <div className="mt-1 text-xs text-rose-400">
              {(update.error as Error).message}
            </div>
          )}
        </div>
      )}

      <Composer
        streaming={streaming}
        onSend={send}
        onStop={() => abortRef.current?.abort()}
      />
    </div>
  );
}
