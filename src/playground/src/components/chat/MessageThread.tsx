import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { ApiError, projectKey, type BudgetScope, type ChatSession, type ChatTranscript } from "../../api";
import { chatSessionsKey } from "../../lib/chatSessions";
import { useUi } from "../../store";
import { useLlmBlock } from "../../lib/permissions";
import { useSlug } from "../../lib/project";
import { BudgetNotice } from "../portal/BudgetNotice";
import { useProjectApi, useProjectKey, useProjectQuery } from "../../lib/project";
import { Loading } from "../Loading";
import { ErrorState } from "../state/ErrorState";
import { Button } from "../ui/button";
import { errorInfo, errorMessage } from "../../lib/apiErrors";
import { isProduction, usePortalState, useRole } from "../../lib/identity";
import { providerLabel } from "../../lib/labels";
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

/** The empty thread's starter questions; a click puts one in the composer (ER-9). */
export const STARTER_PROMPTS = [
  "What changed most in the last month?",
  "Explain how this project is structured",
  "Which areas have no rationale yet?",
];

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
 *
 * `sessionId: null` is a draft (M2f-3 EXC-3): the dropdowns edit a local
 * `{provider, model}`, and the first Send creates the session with it, then posts
 * the message and streams under the new id. `onCreated` tells the view, which
 * keeps this thread mounted when the route moves to `/chat/<id>` (replace), so the
 * stream is never aborted; the reset effect below skips that one transition.
 */
export function MessageThread({
  sessionId,
  session,
  onCreated,
}: {
  /** The open session, or `null` for a new chat that has no session yet. */
  sessionId: number | null;
  /** The session row, for the composer dropdowns. Absent while the list loads. */
  session?: ChatSession;
  /** Called with the id of the session this thread created on its first send. */
  onCreated?: (id: number) => void;
}) {
  const queryClient = useQueryClient();
  const api = useProjectApi();
  const key = useProjectKey();
  const navigate = useNavigate();
  const [liveTurns, setLiveTurns] = useState<Turn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const slug = useSlug();
  const setStreamingSessionId = useUi((s) => s.setStreamingSessionId);
  // The session this thread created (its draft became it), and the draft's own choice.
  const [createdId, setCreatedId] = useState<number | null>(null);
  const createdRef = useRef<number | null>(null);
  const [draft, setDraft] = useState<{ provider: string; model: string } | null>(null);
  const [createError, setCreateError] = useState<unknown>(null);
  // A failed turn is persisted with the provider's raw text; this remembers how the live frame worded
  // it, so the reply does not change wording when the transcript replaces the live copy.
  const [worded, setWorded] = useState<{ raw: string; message: string } | null>(null);
  const [starter, setStarter] = useState<{ text: string; n: number } | null>(null);
  const currentId = sessionId ?? createdId;
  // A hard stop the page has not learned about yet (a live frame, or a 403 on send): the project
  // payload's `llm_block` takes over once it is refetched.
  const llm = useLlmBlock();
  const [stopped, setStopped] = useState<{ scope: BudgetScope | null } | null>(null);
  const blockedScope = llm.block === "budget_exceeded" ? llm.scope : (stopped?.scope ?? null);
  const budgetBlocked = llm.block === "budget_exceeded" || stopped !== null;
  const production = isProduction(usePortalState().data);
  const isOwner = useRole() === "owner";
  const refreshProject = useCallback(
    () => queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") }),
    [queryClient, slug],
  );

  // A draft has no transcript; the session it becomes starts with a seeded empty one
  // (see `send`), so no request lands mid-turn to double the live turn.
  const transcript = useProjectQuery(
    ["chat", "transcript", currentId],
    (api) => api.chatTranscript(currentId as number),
    { enabled: currentId !== null },
  );

  // The draft's pickers start on the first configured provider and its default model.
  const providers = useProjectQuery(["chat", "providers"], (api) => api.chatProviders());
  const defaultProvider = providers.data?.find((p) => p.configured) ?? providers.data?.[0];
  const draftChoice =
    draft ?? (defaultProvider ? { provider: defaultProvider.provider, model: defaultProvider.default_model } : null);
  // An existing session shows its own pair once the list has it; the draft (and the
  // session it just became, until the list catches up) shows the draft's.
  const choice = session ?? (currentId === null || createdId !== null ? draftChoice : null);

  // The provider list failed (ER-4): nothing can be chosen, so nothing is sent until it loads.
  const providersFailed = providers.isError && !providers.data;
  // The chosen provider has no key (EXC-2, MODE-4): the composer is dead and says why.
  const noKeyProvider = choice
    ? (providers.data?.find((p) => p.provider === choice.provider && !p.configured)?.provider ?? null)
    : null;
  const noKeyNotice = noKeyProvider ? (
    <>
      No {providerLabel(noKeyProvider)} key.{" "}
      {production && !isOwner ? (
        "Ask an owner to add one."
      ) : (
        <>
          Add one in{" "}
          <Link to="/settings" className="text-primary-text underline-offset-4 hover:underline">
            Settings &gt; Models and keys
          </Link>
          .
        </>
      )}
    </>
  ) : null;

  const composerNotice = providersFailed
    ? "Chat can't send until the provider list loads. Retry above."
    : noKeyNotice;
  // The composer is disabled or replaced: a starter prompt would fill a box that cannot send.
  const cannotSend = budgetBlocked || noKeyProvider !== null || providersFailed;

  const update = useMutation({
    mutationFn: (vars: { provider?: string; model?: string }) =>
      api.chatUpdateSession(currentId as number, vars),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: chatSessionsKey(slug) });
    },
  });

  // Switching sessions must not carry another session's in-flight turn across. The
  // move from the draft to the session this thread just created is not a switch.
  useEffect(() => {
    if (sessionId !== null && sessionId === createdRef.current) return;
    abortRef.current?.abort();
    abortRef.current = null;
    setLiveTurns([]);
    setStreaming(false);
    setStopped(null);
  }, [sessionId]);

  // Abort an in-flight stream if the view unmounts mid-turn.
  useEffect(() => () => abortRef.current?.abort(), []);

  const persisted = (transcript.data ? turnsFromMessages(transcript.data.messages) : []).map((t) =>
    worded && t.kind === "assistant" && t.error === worded.raw
      ? { ...t, error: worded.message, errorDetail: worded.raw }
      : t,
  );
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
      setCreateError(null);
      setWorded(null);
      setLiveTurns([
        { kind: "user", content },
        emptyAssistantTurn(),
      ]);

      // A draft becomes a session now, on its first message (never on a click).
      let id = currentId;
      if (id === null) {
        try {
          const created = await api.chatCreateSession({
            provider: choice?.provider || undefined,
            model: choice?.model || undefined,
          });
          id = created.id;
        } catch (err) {
          setLiveTurns([]);
          setStreaming(false);
          abortRef.current = null;
          setCreateError(err);
          return;
        }
        // Seeded so the transcript query has rows to show without a request mid-turn.
        queryClient.setQueryData<ChatTranscript>(key("chat", "transcript", id), {
          id,
          title: "",
          provider: choice?.provider ?? "",
          model: choice?.model ?? "",
          created_at: "",
          updated_at: "",
          messages: [],
        });
        createdRef.current = id;
        setCreatedId(id);
        onCreated?.(id);
        void navigate({ to: "/p/$slug/chat/{-$id}", params: { slug, id: String(id) }, replace: true });
      }
      const sid = id;
      setStreamingSessionId(sid);
      let listed = false;

      try {
        await api.streamChat(
          sid,
          content,
          (event) => {
            // The first frame means the server stored the message and titled the
            // session from it: refresh the list so the title shows during the turn.
            if (!listed) {
              listed = true;
              void queryClient.invalidateQueries({ queryKey: chatSessionsKey(slug) });
            }
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
              case "budget_exceeded":
                updateLive((t) => ({
                  ...settleActivities(t),
                  budgetStop: { scope: event.scope, message: event.message },
                }));
                setStopped({ scope: event.scope });
                void refreshProject();
                break;
              case "error":
                // In-band and terminal: HTTP status was committed before the
                // first token, so a provider failure can only arrive this way.
                {
                  const info = errorInfo(
                    new ApiError(502, event.message, event.code, event.provider ? { provider: event.provider } : {}),
                    "chat",
                  );
                  if (info.detail) setWorded({ raw: info.detail, message: info.message });
                  updateLive((t) => ({
                    ...settleActivities(t),
                    error: info.message,
                    errorDetail: info.detail ?? undefined,
                  }));
                }
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
        if (err instanceof ApiError && err.status === 403 && err.code === "budget_exceeded") {
          // Refused before anything was written: show the notice, not an error on a turn.
          const scope = typeof err.extra.scope === "string" ? (err.extra.scope as BudgetScope) : null;
          setStopped({ scope });
          void refreshProject();
          updateLive((t) => ({ ...settleActivities(t), budgetStop: { scope } }));
        } else if ((err as Error).name === "AbortError") {
          updateLive((t) => ({ ...settleActivities(t), error: "Stopped." }));
        } else {
          const info = errorInfo(err, "chat");
          updateLive((t) => ({
            ...settleActivities(t),
            error: info.message,
            errorDetail: info.detail ?? undefined,
          }));
        }
      } finally {
        setStreaming(false);
        if (abortRef.current === controller) abortRef.current = null;
        if (useUi.getState().streamingSessionId === sid) setStreamingSessionId(null);
        // Refetch so the persisted rows replace the local turn — the server
        // persisted as it went, including on abort, so this is authoritative.
        const fresh = await queryClient
          .fetchQuery({
            queryKey: key("chat", "transcript", sid),
            queryFn: () => api.chatTranscript(sid),
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
              t.error ?? "Couldn't refresh the transcript - showing the streamed copy.",
          }));
        }
        // The sidebar shows titles and dates, both of which just moved.
        queryClient.invalidateQueries({ queryKey: chatSessionsKey(slug) });
      }
    },
    [api, choice, currentId, key, navigate, onCreated, queryClient, refreshProject, setStreamingSessionId, slug, updateLive],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-auto p-4">
        {transcript.isLoading && <Loading label="Loading transcript…" />}
        {transcript.isError && (
          <ErrorState
            error={transcript.error}
            context="chat"
            title="Couldn't load this chat"
            onRetry={() => void transcript.refetch()}
          />
        )}
        {!transcript.isLoading && !transcript.isError && turns.length === 0 && (
          <div className="mx-auto flex max-w-xl flex-col items-center gap-4 py-10 text-center" data-testid="chat-empty">
            <div className="flex flex-col gap-1">
              <p className="text-base font-medium text-foreground">Ask about this codebase</p>
              <p className="text-sm text-muted-foreground">
                The assistant reads CodeGraph, the WhyGraph history and the source to answer, and shows the
                tool calls behind each claim.
              </p>
            </div>
            <div className="flex w-full flex-col gap-2">
              {STARTER_PROMPTS.map((text) => (
                <Button
                  key={text}
                  variant="outline"
                  className="h-auto justify-start whitespace-normal py-2 text-left"
                  disabled={cannotSend}
                  onClick={() => setStarter((prev) => ({ text, n: (prev?.n ?? 0) + 1 }))}
                >
                  {text}
                </Button>
              ))}
            </div>
          </div>
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

      {createError !== null && (
        <div className="border-t border-border px-4 py-2">
          <ErrorState size="inline" context="chat" error={createError} className="text-xs" />
        </div>
      )}

      {providersFailed && (
        <div className="border-t border-border px-4 py-2" data-testid="chat-providers-error">
          <ErrorState
            context="chat"
            title="Couldn't load the chat providers"
            error={providers.error}
            onRetry={() => void providers.refetch()}
          />
        </div>
      )}

      {choice && !providersFailed && (
        <div className="border-t border-border px-4 py-2">
          <ModelSelect
            compact
            provider={choice.provider}
            model={choice.model}
            // Streaming is part of this: repointing the session mid-turn would
            // change the row the in-flight turn is attributed to.
            disabled={update.isPending || streaming || budgetBlocked}
            onChange={(next) => {
              if (currentId === null) {
                // A draft: nothing to save yet, the first Send creates the session with it.
                const fallback = providers.data?.find((p) => p.provider === next.provider)?.default_model ?? "";
                setDraft({ provider: next.provider, model: next.model || fallback });
                return;
              }
              update.mutate({
                // Send provider only when it actually changed, so the server
                // doesn't reset the model on a model-only switch.
                provider: next.provider === choice.provider ? undefined : next.provider,
                model: next.model || undefined,
              });
            }}
          />
          {update.isError && (
            <div className="mt-1 text-xs text-destructive">
              {errorMessage(update.error, "chat")}
            </div>
          )}
        </div>
      )}

      {budgetBlocked && !streaming ? (
        <div className="border-t border-border p-3">
          <BudgetNotice scope={blockedScope} testId="chat-budget-notice" />
        </div>
      ) : (
        <Composer
          streaming={streaming}
          fill={starter}
          disabled={noKeyProvider !== null || providersFailed}
          notice={composerNotice}
          disabledPlaceholder={providersFailed ? "Chat providers couldn't load" : undefined}
          onSend={send}
          onStop={() => abortRef.current?.abort()}
        />
      )}
    </div>
  );
}
