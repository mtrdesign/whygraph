import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, streamChat } from "../../api";
import { EmptyState, Spinner } from "../../lib/ui";
import { MessageBubble, type AssistantTurn, type Turn } from "./MessageBubble";
import { Composer } from "./Composer";
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
 */
export function MessageThread({ sessionId }: { sessionId: number }) {
  const queryClient = useQueryClient();
  const [liveTurns, setLiveTurns] = useState<Turn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const transcript = useQuery({
    queryKey: ["chat", "transcript", sessionId],
    queryFn: () => api.chatTranscript(sessionId),
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
          })
          .catch(() => null);
        if (fresh) setLiveTurns([]);
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
        {turns.map((turn, i) => (
          <MessageBubble key={i} turn={turn} />
        ))}
      </div>

      <Composer
        streaming={streaming}
        onSend={send}
        onStop={() => abortRef.current?.abort()}
      />
    </div>
  );
}
