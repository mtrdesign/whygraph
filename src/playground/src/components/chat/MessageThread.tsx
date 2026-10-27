import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import {
  ApiError,
  projectKey,
  type BudgetScope,
  type ChatEvent,
  type ChatSession,
  type ChatTranscript,
} from "../../api";
import { chatSessionsKey } from "../../lib/chatSessions";
import { useUi } from "../../store";
import { useLlmBlock, useProjectCan } from "../../lib/permissions";
import { useSlug } from "../../lib/project";
import { BudgetNotice } from "../portal/BudgetNotice";
import { useProjectApi, useProjectKey, useProjectQuery } from "../../lib/project";
import { Loading } from "../Loading";
import { EmptyState } from "../state/EmptyState";
import { ErrorState } from "../state/ErrorState";
import { Button } from "../ui/button";
import { errorInfo, errorMessage } from "../../lib/apiErrors";
import { isProduction, usePortalState, useRole } from "../../lib/identity";
import { providerLabel } from "../../lib/labels";
import { MessageBubble, type AssistantTurn, type Turn, type UserTurn } from "./MessageBubble";
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
 * How long after the person pressed Stop a "turn in progress" answer is theirs to wait out: the
 * server cannot interrupt a running tool call, so the stopped turn may take a while to let go of
 * the session. Past this window the answer is an ordinary error.
 */
export const STOP_GRACE_MS = 120_000;
const STOP_POLL_MS = 1_000;

/** Resolve after `ms`, or reject with an AbortError the moment `signal` aborts. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const aborted = () => reject(new DOMException("Aborted", "AbortError"));
    if (signal.aborted) return aborted();
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      aborted();
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** One turn about to run: what the screen shows meanwhile, and how its stream is opened. */
interface TurnStart {
  kind: "send" | "edit" | "regenerate";
  /** How many persisted turns stay on screen while it runs (`null`: all of them). */
  keep: number | null;
  /** The optimistic live view; the persisted rows replace it when the turn ends. */
  live: Turn[];
  open: (sid: number, onEvent: (event: ChatEvent) => void, signal: AbortSignal) => Promise<void>;
}

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
  // An edit or regenerate hides the persisted turns it replaces (they return as versions once the
  // transcript is refetched): only this many persisted turns are shown while it runs.
  const [keep, setKeep] = useState<number | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [branching, setBranching] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  // When the person last pressed Stop in this thread (see `STOP_GRACE_MS`).
  const stoppedAtRef = useRef<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const slug = useSlug();
  const setStreamingSessionId = useUi((s) => s.setStreamingSessionId);
  // The session this thread created (its draft became it), and the draft's own choice.
  const [createdId, setCreatedId] = useState<number | null>(null);
  const createdRef = useRef<number | null>(null);
  const [draft, setDraft] = useState<{ provider: string; model: string } | null>(null);
  // A failure with no turn to hang it on: the draft's session could not be created, an edit or regenerate
  // was turned away before it wrote anything, or a version switch failed.
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

  // The draft's pickers start on the server's default provider (the Chat setting when it has a key,
  // else the first provider that has one) and its default model; none when no provider has a key.
  const providers = useProjectQuery(["chat", "providers"], (api) => api.chatProviders(), {
    refetchOnWindowFocus: true,
  });
  const defaultProvider = providers.data?.find((p) => p.default) ?? null;
  const draftChoice =
    draft ?? (defaultProvider ? { provider: defaultProvider.provider, model: defaultProvider.default_model } : null);
  // An existing session shows its own pair once the list has it; the draft (and the
  // session it just became, until the list catches up) shows the draft's.
  const choice = session ?? (currentId === null || createdId !== null ? draftChoice : null);

  // The provider list failed (ER-4): nothing can be chosen, so nothing is sent until it loads.
  const providersFailed = providers.isError && !providers.data;
  // No provider has a key at all: a new chat cannot start, so the draft says so instead of a composer.
  const noKeys = !!providers.data && !providers.data.some((p) => p.configured);
  const needsKey = noKeys && currentId === null;
  // The chosen provider has no key (EXC-2, MODE-4): the composer is dead and says why.
  const noKeyProvider = choice
    ? (providers.data?.find((p) => p.provider === choice.provider && !p.configured)?.provider ?? null)
    : null;
  // Who can add a key: project admins (and owners) hold `project.configure`; locally everyone does.
  const mayConfigure = useProjectCan("project.configure");
  const canFix = !production || mayConfigure;
  const fixLine = canFix ? (
    <>
      Add one in{" "}
      <Link
        to="/p/$slug/settings"
        params={{ slug }}
        search={{ section: "models" }}
        className="text-primary-text underline-offset-4 hover:underline"
      >
        this project&apos;s Settings
      </Link>
      {(!production || isOwner) && (
        <>
          {" "}
          or in the{" "}
          <Link
            to="/settings"
            search={{ section: "models" }}
            className="text-primary-text underline-offset-4 hover:underline"
          >
            {production ? "organization's Settings" : "portal's Settings"}
          </Link>
        </>
      )}
      .
    </>
  ) : (
    "Ask a project admin or an owner to add one."
  );
  const noKeyNotice = noKeyProvider ? (
    <>
      No {providerLabel(noKeyProvider)} key. {fixLine}
    </>
  ) : null;

  const composerNotice = providersFailed
    ? "Chat can't send until the provider list loads. Retry above."
    : noKeyNotice;
  // The composer is disabled or replaced: a starter prompt would fill a box that cannot send.
  const cannotSend = budgetBlocked || noKeys || noKeyProvider !== null || providersFailed;

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
    setKeep(null);
    setStreaming(false);
    setStopped(null);
    stoppedAtRef.current = null;
  }, [sessionId]);

  // Abort an in-flight stream if the view unmounts mid-turn.
  useEffect(() => () => abortRef.current?.abort(), []);

  const persisted = (
    transcript.data ? turnsFromMessages(transcript.data.messages, transcript.data.versions) : []
  ).map((t) =>
    worded && t.kind === "assistant" && t.error === worded.raw
      ? { ...t, error: worded.message, errorDetail: worded.raw }
      : t,
  );
  const shown = keep === null ? persisted : persisted.slice(0, keep);
  const turns = [...shown, ...liveTurns];
  // Nothing may start, switch or edit while a turn is in flight.
  const busy = streaming || branching;
  // Regenerate belongs to the last assistant turn only, and only once it is persisted: it needs the
  // question's id. (A later step may offer it on any turn.)
  const lastTurn = turns[turns.length - 1];
  const lastQuestion = turns[turns.length - 2];
  const regenerableIndex =
    liveTurns.length === 0 &&
    lastTurn?.kind === "assistant" &&
    lastTurn.id !== undefined &&
    lastQuestion?.kind === "user" &&
    lastQuestion.id !== undefined
      ? turns.length - 1
      : -1;

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

  // Send, edit and regenerate are the same thing to the screen: a turn that streams in, and a
  // transcript that replaces it. They differ only in what stays on screen meanwhile (`start.keep`,
  // `start.live`) and in the route that opens the stream (`start.open`).
  const runTurn = useCallback(
    async (start: TurnStart) => {
      // Started from the click/Enter handler, never an effect — StrictMode
      // double-invokes effects in dev, which would send the turn twice.
      const controller = new AbortController();
      abortRef.current = controller;
      setStreaming(true);
      setCreateError(null);
      setWorded(null);
      setKeep(start.keep);
      setLiveTurns(start.live);

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
          // No key after all (changed elsewhere): refetch so the draft flips to the no-key state.
          if (err instanceof ApiError && err.status === 409 && err.code === "no_llm_key") {
            void queryClient.invalidateQueries({ queryKey: key("chat", "providers") });
          }
          setLiveTurns([]);
          setKeep(null);
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
      // The server refused the send (no key, or a turn already running): the live turn stays, with the reason.
      let refused = false;

      // The person pressed Stop a moment ago and the stopped turn still holds the session.
      const recentlyStopped = () =>
        stoppedAtRef.current !== null && Date.now() - stoppedAtRef.current < STOP_GRACE_MS;

      try {
        const onEvent = (event: ChatEvent) => {
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
            case "continuation":
              // The output limit cut the reply and it goes on: the text keeps appending to this bubble.
              break;
            case "empty_reply":
              // The model was nudged once after saying nothing; the turn stays live and nothing shows yet.
              break;
            case "context_trimmed":
              updateLive((t) => ({ ...t, trimmed: true }));
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
                finishReason: event.finish_reason ?? undefined,
              }));
              break;
          }
        };

        // A `turn_in_progress` right after the person's own Stop is the stopped turn still winding
        // down: say so, poll the transcript until the session lets go, then send (bounded by the window).
        for (;;) {
          try {
            await start.open(sid, onEvent, controller.signal);
            break;
          } catch (err) {
            const busyAnswer = err instanceof ApiError && err.status === 409 && err.code === "turn_in_progress";
            if (!busyAnswer || !recentlyStopped() || controller.signal.aborted) throw err;
            updateLive((t) => ({ ...t, statusNote: "Stopping the previous reply…" }));
            do {
              await pause(STOP_POLL_MS, controller.signal);
              if (!recentlyStopped()) throw err;
            } while ((await api.chatTranscript(sid)).streaming);
            updateLive((t) => ({ ...t, statusNote: undefined }));
          }
        }
      } catch (err) {
        if (start.kind !== "send" && !listed && err instanceof ApiError) {
          // Turned away before anything was written (a preflight refusal, or a 409 that outlived the
          // wait): there is no turn to hang the reason on, and the persisted turns the optimistic view
          // replaced are still the truth - put them back and say why in the strip below.
          if (err.status === 403 && err.code === "budget_exceeded") {
            setStopped({ scope: typeof err.extra.scope === "string" ? (err.extra.scope as BudgetScope) : null });
            void refreshProject();
          } else {
            if (err.status === 409 && err.code === "no_llm_key") {
              void queryClient.invalidateQueries({ queryKey: key("chat", "providers") });
            }
            setCreateError(err);
          }
          setLiveTurns([]);
          setKeep(null);
        } else if (err instanceof ApiError && err.status === 403 && err.code === "budget_exceeded") {
          // Refused before anything was written: show the notice, not an error on a turn.
          const scope = typeof err.extra.scope === "string" ? (err.extra.scope as BudgetScope) : null;
          setStopped({ scope });
          void refreshProject();
          updateLive((t) => ({ ...settleActivities(t), budgetStop: { scope } }));
        } else if (
          err instanceof ApiError &&
          err.status === 409 &&
          (err.code === "no_llm_key" || err.code === "turn_in_progress")
        ) {
          // Turned away with nothing written (`turn_in_progress`: another turn holds the session and it
          // was not the person's own Stop): the live turn stays, with the reason.
          refused = true;
          if (err.code === "no_llm_key") void queryClient.invalidateQueries({ queryKey: key("chat", "providers") });
          const info = errorInfo(err, "chat");
          updateLive((t) => ({
            ...settleActivities(t),
            error: info.message,
            errorDetail: info.detail ?? undefined,
          }));
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
        // A refused send wrote nothing, so the transcript has no copy of it: keep the live turn.
        if (fresh && !refused) {
          setLiveTurns([]);
          setKeep(null);
        } else if (!fresh) {
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

  const send = (content: string) =>
    runTurn({
      kind: "send",
      keep: null,
      live: [{ kind: "user", content }, emptyAssistantTurn()],
      open: (sid, onEvent, signal) => api.streamChat(sid, content, onEvent, signal),
    });

  // A sibling version of a persisted question: the turns from it on are hidden until the refetch.
  const edit = (turn: UserTurn, content: string) => {
    const index = persisted.findIndex((t) => t.kind === "user" && t.id === turn.id);
    if (currentId === null || turn.id === undefined || index < 0) return;
    const messageId = turn.id;
    void runTurn({
      kind: "edit",
      keep: index,
      live: [{ kind: "user", content }, emptyAssistantTurn()],
      open: (sid, onEvent, signal) => api.chatEditMessage(sid, messageId, content, onEvent, signal),
    });
  };

  // A new reply to the last question: the old reply is hidden until the refetch brings it back as a version.
  const regenerate = () => {
    const question = shown[shown.length - 2];
    if (currentId === null || regenerableIndex < 0 || question?.kind !== "user" || question.id === undefined) return;
    const messageId = question.id;
    void runTurn({
      kind: "regenerate",
      keep: shown.length - 1,
      live: [emptyAssistantTurn()],
      open: (sid, onEvent, signal) => api.chatRegenerate(sid, messageId, onEvent, signal),
    });
  };

  // Switch the visible version: the server moves the leaf and answers with the new path.
  const branchTo = async (messageId: number) => {
    if (currentId === null) return;
    setBranching(true);
    setCreateError(null);
    try {
      const next = await api.chatBranch(currentId, messageId);
      queryClient.setQueryData<ChatTranscript>(key("chat", "transcript", currentId), next);
    } catch (err) {
      setCreateError(err);
    } finally {
      setBranching(false);
    }
  };

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
        {needsKey && (
          <div data-testid="chat-needs-key">
            <EmptyState
              title="Chat needs an LLM key"
              description={<>Chat runs on a provider key stored in Settings. {fixLine}</>}
            />
          </div>
        )}
        {!needsKey && !transcript.isLoading && !transcript.isError && turns.length === 0 && (
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
        {turns.map((turn, i) => (
          <MessageBubble
            key={turn.id !== undefined ? `row-${turn.id}` : `live-${turn.kind}`}
            turn={turn}
            busy={busy}
            onEdit={
              turn.kind === "user" && turn.id !== undefined && currentId !== null && !cannotSend
                ? (content) => edit(turn, content)
                : undefined
            }
            onRegenerate={i === regenerableIndex && !cannotSend ? regenerate : undefined}
            onBranch={(id) => void branchTo(id)}
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

      {needsKey ? null : budgetBlocked && !streaming ? (
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
          onStop={() => {
            stoppedAtRef.current = Date.now();
            abortRef.current?.abort();
          }}
        />
      )}
    </div>
  );
}
