import { Suspense, lazy, useEffect, useRef, useState } from "react";
import {
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
  GitBranchIcon,
  PencilIcon,
  RefreshCwIcon,
  ThumbsDownIcon,
  ThumbsUpIcon,
} from "lucide-react";
import type { ChatVersions } from "../../api";
import { BudgetNotice } from "../portal/BudgetNotice";
import { writeClipboard } from "../portal/CopyButton";
import { parseChart } from "../charts/chartSpec";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { Textarea } from "../ui/textarea";
import { formatNumber } from "../../lib/format";
import { Markdown } from "./Markdown";
import { ProposedNoteCard } from "./ProposedNoteCard";
import { ToolCallCard, type ToolActivity } from "./ToolCallCard";

// ECharts is ~196 KB gzipped even fully tree-shaken — measured, against a vendor
// claim of ~100 KB — and a transcript with no chart in it should not pay for that.
// Charts are conditional UI, so the whole renderer loads on first use and the
// initial bundle grows by ~1 KB instead of ~575 KB. `chartSpec.ts` stays eager: it
// decides *whether* there is a chart, and it imports nothing heavy.
const ChartBlock = lazy(() =>
  import("../charts/ChartBlock").then((module) => ({ default: module.ChartBlock })),
);

/**
 * One conversational turn: the user's question, or the assistant's reply with
 * its tool activity interleaved.
 *
 * A "turn" here is a display unit, not a DB row — an assistant turn that called
 * tools is several rows (assistant → tool results → assistant), and this
 * renders them as one bubble with cards in the middle, which is how the user
 * experienced it live.
 */
export interface AssistantTurn {
  kind: "assistant";
  /**
   * Id of the first persisted row this turn was built from — the React key, so
   * a turn keeps its identity across the live→persisted swap at end of turn
   * (index keys would hand one card's expansion state to another). Absent on
   * live turns, which key off their position instead.
   */
  id?: number;
  /**
   * Persisted turns only: the id of the turn's last row (tool rows included), what "Branch from
   * here" copies up to so the whole reply comes along.
   */
  lastId?: number;
  /**
   * The id of the row a rating goes on: the turn's last assistant row, when that row ends the turn
   * (no tool calls, or a failure; a later tool row or continuation clears it). Absent: no rating control.
   */
  ratingId?: number;
  /** The stored rating of `ratingId`: 1 (up) or -1 (down); absent when unrated. */
  rating?: 1 | -1;
  ratingReason?: string;
  ratingComment?: string;
  /** Text segments in order, interleaved with `activities` of the same index. */
  segments: string[];
  activityGroups: ToolActivity[][];
  /**
   * The model's reasoning per round, at the index of the segment it precedes. Shown collapsed,
   * never copied.
   */
  reasoning?: string[];
  usage?: { input: number | null; output: number | null };
  /** Which model produced this turn — shown because it can differ per turn. */
  model?: string | null;
  error?: string;
  /** The provider's or server's own text behind `error`, for "Show details" (ER-3). */
  errorDetail?: string;
  /** The monthly budget's hard stop ended this turn (a live frame or a persisted row). */
  budgetStop?: { scope?: string | null; message?: string };
  roundLimit?: number;
  /**
   * Why the provider stopped the turn's last row: `max_tokens` / `length` (the output limit) and
   * `refusal` / `content_filter` (declined) get a notice; anything else shows nothing.
   */
  finishReason?: string;
  /**
   * Persisted turns only: the id of the turn's last row when that row is an assistant row the output
   * limit cut off - the one the server will continue. Absent when the turn ended any other way.
   */
  continuableId?: number;
  /** Older tool results of this turn were elided to fit the context. Live turns only: a transcript cannot know. */
  trimmed?: boolean;
  /** The turn's versions (it has siblings in the tree), looked up by its first row. */
  versions?: ChatVersions;
  /** A transient line shown in place of "Thinking…" (e.g. waiting for a stopped reply to wind down). */
  statusNote?: string;
  /**
   * Transient: the model is working and has nothing on screen yet — the
   * pre-first-token prologue, or the gap between a tool result and the next
   * round. Live turns only; `turnsFromMessages` never sets it, so a replayed
   * transcript can't show a stuck indicator.
   */
  thinking?: boolean;
}

export interface UserTurn {
  kind: "user";
  content: string;
  /** See `AssistantTurn.id`. */
  id?: number;
  /** The question's edited versions (it has siblings in the tree). */
  versions?: ChatVersions;
}

export type Turn = UserTurn | AssistantTurn;

/**
 * What a bubble can do. Every handler is optional (a bubble with none renders no actions, which is
 * how the live turn and the old fixtures stay as they were), and `busy` hides all of them and the
 * version arrows while a turn streams: nothing may start a second turn on the session.
 */
export interface TurnActions {
  busy?: boolean;
  /** User turns: save an edited question as a new version. */
  onEdit?: (content: string) => void;
  /** Passed for the LAST assistant turn only: write its reply again. */
  onRegenerate?: () => void;
  /** Passed for the LAST assistant turn only, when the output limit cut it off: carry on from there. */
  onContinue?: () => void;
  /** Persisted turns: start a new chat with the conversation up to and including this turn. */
  onFork?: () => void;
  /**
   * Persisted turns with a `ratingId`: save a rating (`1` / `-1` with an optional reason and comment),
   * or clear it (`null`). Rejects when the server refuses.
   */
  onRate?: (rating: { rating: 1 | -1; reason?: string; comment?: string } | null) => Promise<void> | void;
  /** Version arrows: switch to the sibling with this message id. */
  onBranch?: (messageId: number) => void;
}

const ACTION_ROW = "relative mt-0.5 flex h-6 items-center gap-0.5";
// Hover-revealed, but never hidden from a keyboard (focus-within) or a touch screen (no hover).
const REVEAL =
  "flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100";

function CopyAction({ text, what }: { text: string; what: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <Button
      type="button"
      size="icon-xs"
      variant="ghost"
      aria-label={`Copy ${what}`}
      title={copied ? "Copied" : `Copy ${what}`}
      onClick={async () => {
        if (!(await writeClipboard(text))) return;
        setCopied(true);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
    </Button>
  );
}

function ForkAction({ onFork }: { onFork: () => void }) {
  return (
    <Button
      type="button"
      size="icon-xs"
      variant="ghost"
      aria-label="Branch from here"
      title="Branch from here"
      onClick={onFork}
    >
      <GitBranchIcon />
    </Button>
  );
}

const RATING_REASONS: { id: string; label: string }[] = [
  { id: "wrong", label: "Wrong" },
  { id: "incomplete", label: "Incomplete" },
  { id: "not_grounded", label: "Not grounded" },
  { id: "too_slow", label: "Too slow" },
  { id: "other", label: "Other" },
];

/**
 * Thumbs up / down on a finished reply. Down opens a small panel (a reason and an optional comment);
 * clicking the active thumb clears the rating. The comment is typed into a textarea and shown nowhere
 * else here, so it is only ever text.
 */
function RateActions({
  rating,
  reason,
  onRate,
}: {
  rating?: 1 | -1;
  reason?: string;
  onRate: NonNullable<TurnActions["onRate"]>;
}) {
  const [current, setCurrent] = useState<1 | -1 | undefined>(rating);
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState<string | undefined>(reason);
  const [comment, setComment] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => setCurrent(rating), [rating]);

  const save = async (next: { rating: 1 | -1; reason?: string; comment?: string } | null) => {
    setFailed(false);
    try {
      await onRate(next);
      setCurrent(next?.rating);
      setOpen(false);
    } catch {
      setFailed(true);
    }
  };

  return (
    <>
      <Button
        type="button"
        size="icon-xs"
        variant="ghost"
        aria-label="Good answer"
        aria-pressed={current === 1}
        title="Good answer"
        className={current === 1 ? "text-foreground" : undefined}
        onClick={() => void save(current === 1 ? null : { rating: 1 })}
      >
        <ThumbsUpIcon />
      </Button>
      <Button
        type="button"
        size="icon-xs"
        variant="ghost"
        aria-label="Bad answer"
        aria-pressed={current === -1}
        title="Bad answer"
        className={current === -1 ? "text-foreground" : undefined}
        onClick={() => (current === -1 ? void save(null) : setOpen((v) => !v))}
      >
        <ThumbsDownIcon />
      </Button>
      {open && (
        <div
          className="absolute left-0 top-7 z-10 flex w-64 flex-col gap-2 rounded-md border border-border bg-popover p-2 text-xs shadow-md"
          data-testid="rating-popover"
        >
          <div className="flex flex-wrap gap-1">
            {RATING_REASONS.map((r) => (
              <Button
                key={r.id}
                type="button"
                size="xs"
                variant={chosen === r.id ? "secondary" : "outline"}
                aria-pressed={chosen === r.id}
                onClick={() => setChosen(chosen === r.id ? undefined : r.id)}
              >
                {r.label}
              </Button>
            ))}
          </div>
          <Textarea
            aria-label="What went wrong (optional)"
            placeholder="What went wrong? (optional)"
            maxLength={500}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
          />
          {failed && <span className="text-destructive">Couldn't save the rating.</span>}
          <div className="flex justify-end gap-1">
            <Button type="button" size="xs" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              size="xs"
              onClick={() =>
                void save({ rating: -1, reason: chosen, comment: comment.trim() ? comment.trim() : undefined })
              }
            >
              Send
            </Button>
          </div>
        </div>
      )}
    </>
  );
}

/** `‹ 2/3 ›`: step between the versions of a message. Rendered only when there is more than one. */
export function VersionArrows({
  versions,
  onBranch,
}: {
  versions: ChatVersions;
  onBranch: (messageId: number) => void;
}) {
  const { index, count, siblings } = versions;
  return (
    <div className="flex items-center text-[10px] text-muted-foreground" data-testid="version-arrows">
      <Button
        type="button"
        size="icon-xs"
        variant="ghost"
        aria-label="Previous version"
        disabled={index <= 1}
        onClick={() => onBranch(siblings[index - 2])}
      >
        <ChevronLeftIcon />
      </Button>
      <span className="tabular-nums" aria-label={`Version ${index} of ${count}`}>
        {index}/{count}
      </span>
      <Button
        type="button"
        size="icon-xs"
        variant="ghost"
        aria-label="Next version"
        disabled={index >= count}
        onClick={() => onBranch(siblings[index])}
      >
        <ChevronRightIcon />
      </Button>
    </div>
  );
}

function UserBubble({ turn, busy, onEdit, onFork, onBranch }: { turn: UserTurn } & TurnActions) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(turn.content);

  const save = () => {
    const next = draft.trim();
    setEditing(false);
    // Unchanged is a cancel: saving it would only add a twin version.
    if (!next || next === turn.content.trim() || busy) return;
    onEdit?.(next);
  };

  if (editing) {
    return (
      <div className="flex justify-end">
        <div className="w-full max-w-[80%]">
          <Textarea
            autoFocus
            rows={3}
            className="min-h-0"
            aria-label="Edit message"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                save();
              } else if (e.key === "Escape") {
                e.preventDefault();
                setEditing(false);
              }
            }}
          />
          <div className="mt-1 flex items-center justify-end gap-2">
            <span className="text-[10px] text-muted-foreground">Enter to save · Esc to cancel</span>
            <Button type="button" size="xs" variant="outline" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button type="button" size="xs" onClick={save} disabled={busy || !draft.trim()}>
              Save
            </Button>
          </div>
        </div>
      </div>
    );
  }

  const arrows = !busy && onBranch && turn.versions && turn.versions.count > 1 ? turn.versions : null;
  const actions = !busy && !!turn.content;
  return (
    <div className="group flex justify-end">
      <div className="flex min-w-0 max-w-[80%] flex-col items-end">
        <div className="whitespace-pre-wrap rounded-lg bg-primary-soft px-3 py-2 text-sm text-primary-text">
          {turn.content}
        </div>
        {(arrows || actions) && (
          <div className={ACTION_ROW}>
            {arrows && <VersionArrows versions={arrows} onBranch={onBranch!} />}
            {actions && (
              <div className={REVEAL}>
                <CopyAction text={turn.content} what="message" />
                {onEdit && (
                  <Button
                    type="button"
                    size="icon-xs"
                    variant="ghost"
                    aria-label="Edit message"
                    title="Edit message"
                    onClick={() => {
                      setDraft(turn.content);
                      setEditing(true);
                    }}
                  >
                    <PencilIcon />
                  </Button>
                )}
                {onFork && <ForkAction onFork={onFork} />}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

const REASONING_OPEN_KEY = "whygraph.chat.reasoningOpen";

/** Whether this viewer last left reasoning blocks open (a per-viewer convenience). */
function readReasoningOpen(): boolean {
  try {
    return window.localStorage.getItem(REASONING_OPEN_KEY) === "1";
  } catch {
    return false;
  }
}

function storeReasoningOpen(open: boolean): void {
  try {
    window.localStorage.setItem(REASONING_OPEN_KEY, open ? "1" : "0");
  } catch {
    // Private windows and blocked storage: the block still works, it just isn't remembered.
  }
}

/** One round's reasoning, collapsed unless this viewer last left it open. */
export function ReasoningBlock({ text }: { text: string }) {
  return (
    <details
      className="mb-1.5 rounded-sm border border-border/60 bg-background/40 px-2 py-1 text-xs text-muted-foreground"
      open={readReasoningOpen()}
      onToggle={(e) => storeReasoningOpen(e.currentTarget.open)}
      data-testid="reasoning"
    >
      <summary className="cursor-pointer select-none hover:text-foreground">Reasoning</summary>
      <p className="mt-1 whitespace-pre-wrap break-words">{text}</p>
    </details>
  );
}

function AssistantBubble({
  turn,
  busy,
  onRegenerate,
  onContinue,
  onFork,
  onRate,
  onBranch,
}: { turn: AssistantTurn } & TurnActions) {
  const rows = Math.max(turn.segments.length, turn.activityGroups.length);
  // Flattened across groups on purpose. `render_chart` and the stats call that
  // minted its `chart_ref` are separate tool *rounds*, and a new round opens a new
  // group — always on replay, and live too whenever the model says something
  // between them. Searching only the chart's own group would find the producer
  // exactly when the two happened to share a round, which is the case that does
  // not survive a reload.
  const turnActivities = turn.activityGroups.flat();
  // Settled: not waiting on the model and no tool still running. A settled
  // turn with no text said nothing - with or without tool cards - and must
  // say so rather than look like it is still thinking.
  const settled = !turn.thinking && turn.activityGroups.every((g) => g.every((a) => !a.running));
  const noReply =
    settled && !turn.error && !turn.budgetStop && turn.segments.every((s) => !s.trim());
  // Copy takes the turn's text only: tool cards and notices are not part of the answer, and the
  // reasoning never reaches a turn.
  const copyText = turn.segments.filter((t) => t.trim()).join("\n\n");
  const arrows = !busy && onBranch && turn.versions && turn.versions.count > 1 ? turn.versions : null;
  // The no-reply line carries its own Regenerate, so the hover one would be a twin.
  const regenerate = !!onRegenerate && !noReply;
  // A turn still thinking or running a tool has nothing to act on yet; `busy` covers the rest.
  const rate = turn.ratingId !== undefined && !!onRate;
  const actions = !busy && settled && (!!copyText || regenerate || !!onFork || rate);

  return (
    <div className="group flex min-w-0 max-w-[92%] flex-col items-start">
      <div className="min-w-0 max-w-full rounded-lg bg-muted px-3 py-2">
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="min-w-0">
            {turn.reasoning?.[i] ? <ReasoningBlock text={turn.reasoning[i]} /> : null}
            {turn.segments[i] ? <Markdown>{turn.segments[i]}</Markdown> : null}
            {(turn.activityGroups[i] ?? []).map((activity) => {
              // A `render_chart` card renders its chart as a sibling, so the
              // "see exactly what the assistant looked at" guarantee survives.
              // The whole turn is passed because the rows live on the producing
              // stats activity, matched by `chart_ref`.
              const chart = parseChart(activity, turnActivities);
              return (
                // The key stays on the wrapper: dropping it hands one card's
                // expansion state to another as the transcript re-renders.
                <div key={activity.id}>
                  <ToolCallCard activity={activity} />
                  {activity.name === "propose_note" && !activity.running && (
                    <ProposedNoteCard result={activity.result} />
                  )}
                  {chart && (
                    <Suspense
                      fallback={
                        <Skeleton
                          role="status"
                          aria-label="Loading chart"
                          className="my-1.5 h-24 w-full bg-foreground/10"
                        />
                      }
                    >
                      <ChartBlock payload={chart} />
                    </Suspense>
                  )}
                </div>
              );
            })}
          </div>
        ))}

        {/* `thinking` is live-only: the pre-first-token prologue and the gap
            between a tool result and the next round. */}
        {turn.thinking && (
          <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="animate-pulse">{turn.statusNote ?? "Thinking…"}</span>
          </div>
        )}

        {noReply && (
          <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground" data-testid="no-reply">
            The model ended its turn without a reply.
            {!busy && onRegenerate && (
              <Button type="button" size="xs" variant="outline" onClick={onRegenerate}>
                <RefreshCwIcon data-icon="inline-start" />
                Regenerate
              </Button>
            )}
          </div>
        )}

        {turn.roundLimit !== undefined && (
          <div className="mt-2 rounded-sm border border-warning/30 bg-warning/10 px-2 py-1 text-xs text-warning">
            Reached the {turn.roundLimit}-round tool limit - the assistant answered
            with what it had gathered. Ask a narrower question to go further.
          </div>
        )}

        {(turn.finishReason === "max_tokens" || turn.finishReason === "length") && (
          <div
            className="mt-2 rounded-sm border border-warning/30 bg-warning/10 px-2 py-1 text-xs text-warning"
            data-testid="output-limit-notice"
          >
            The reply hit the output limit.
            {!busy && onContinue && (
              <Button type="button" size="xs" variant="outline" className="ml-2" onClick={onContinue}>
                Continue
              </Button>
            )}
          </div>
        )}

        {(turn.finishReason === "refusal" || turn.finishReason === "content_filter") && (
          <div
            className="mt-2 rounded-sm border border-warning/30 bg-warning/10 px-2 py-1 text-xs text-warning"
            data-testid="declined-notice"
          >
            The provider declined to answer.
          </div>
        )}

        {turn.trimmed && (
          <div
            className="mt-2 rounded-sm border border-warning/30 bg-warning/10 px-2 py-1 text-xs text-warning"
            data-testid="trimmed-notice"
          >
            Older tool results in this turn were shortened to fit the context.
          </div>
        )}

        {turn.budgetStop && (
          <BudgetNotice
            testId="budget-stop-row"
            scope={turn.budgetStop.scope}
            message={turn.budgetStop.message}
            className="mt-2 px-2 py-1 text-xs"
          />
        )}

        {turn.error && (
          <div className="mt-2 rounded-sm border border-destructive/30 bg-destructive/10 px-2 py-1 text-xs text-destructive">
            {turn.error}
            {turn.errorDetail && turn.errorDetail !== turn.error && (
              <details className="mt-1 text-muted-foreground" data-testid="error-details">
                <summary className="cursor-pointer select-none hover:text-foreground">Show details</summary>
                <p className="mt-1 break-words whitespace-pre-wrap">{turn.errorDetail}</p>
              </details>
            )}
          </div>
        )}

        {(turn.model || (turn.usage && (turn.usage.input || turn.usage.output))) && (
          <div className="mt-2 flex items-center gap-2 text-[10px] text-muted-foreground">
            {turn.model && <span className="font-mono">{turn.model}</span>}
            {turn.model && turn.usage && (turn.usage.input || turn.usage.output) && <span aria-hidden>·</span>}
            {turn.usage && (turn.usage.input || turn.usage.output) && (
              <span data-testid="turn-tokens">
                {turn.usage.input == null ? "?" : formatNumber(turn.usage.input)} in
                {" · "}
                {turn.usage.output == null ? "?" : formatNumber(turn.usage.output)} out
              </span>
            )}
          </div>
        )}
      </div>

      {(arrows || actions) && (
        <div className={ACTION_ROW}>
          {arrows && <VersionArrows versions={arrows} onBranch={onBranch!} />}
          {actions && (
            <div className={REVEAL}>
              {copyText && <CopyAction text={copyText} what="reply" />}
              {regenerate && (
                <Button
                  type="button"
                  size="icon-xs"
                  variant="ghost"
                  aria-label="Regenerate reply"
                  title="Regenerate reply"
                  onClick={onRegenerate}
                >
                  <RefreshCwIcon />
                </Button>
              )}
              {onFork && <ForkAction onFork={onFork} />}
              {rate && <RateActions rating={turn.rating} reason={turn.ratingReason} onRate={onRate!} />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function MessageBubble({ turn, ...actions }: { turn: Turn } & TurnActions) {
  return turn.kind === "user" ? (
    <UserBubble turn={turn} {...actions} />
  ) : (
    <AssistantBubble turn={turn} {...actions} />
  );
}
