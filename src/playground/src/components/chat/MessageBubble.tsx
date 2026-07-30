import { Markdown } from "./Markdown";
import { ToolCallCard, type ToolActivity } from "./ToolCallCard";

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
  /** Text segments in order, interleaved with `activities` of the same index. */
  segments: string[];
  activityGroups: ToolActivity[][];
  usage?: { input: number | null; output: number | null };
  /** Which model produced this turn — shown because it can differ per turn. */
  model?: string | null;
  error?: string;
  roundLimit?: number;
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
}

export type Turn = UserTurn | AssistantTurn;

function UserBubble({ content }: { content: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] whitespace-pre-wrap rounded-lg bg-accent/20 px-3 py-2 text-sm text-fg">
        {content}
      </div>
    </div>
  );
}

function AssistantBubble({ turn }: { turn: AssistantTurn }) {
  const rows = Math.max(turn.segments.length, turn.activityGroups.length);
  const isEmpty =
    !turn.error &&
    turn.segments.every((s) => !s) &&
    turn.activityGroups.every((g) => g.length === 0);

  return (
    <div className="flex justify-start">
      <div className="min-w-0 max-w-[92%] rounded-lg bg-panel2 px-3 py-2">
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="min-w-0">
            {turn.segments[i] ? <Markdown>{turn.segments[i]}</Markdown> : null}
            {(turn.activityGroups[i] ?? []).map((activity) => (
              <ToolCallCard key={activity.id} activity={activity} />
            ))}
          </div>
        ))}

        {/* `isEmpty` covers a bubble with nothing in it at all; `thinking` also
            covers the inter-round gap, where segments and cards are present but
            the model hasn't spoken yet. */}
        {(turn.thinking || isEmpty) && (
          <div className="mt-1 flex items-center gap-1.5 text-xs text-muted">
            <span className="animate-pulse">Thinking…</span>
          </div>
        )}

        {turn.roundLimit !== undefined && (
          <div className="mt-2 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-xs text-amber-300">
            Reached the {turn.roundLimit}-round tool limit — the assistant answered
            with what it had gathered. Ask a narrower question to go further.
          </div>
        )}

        {turn.error && (
          <div className="mt-2 rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-xs text-rose-300">
            {turn.error}
          </div>
        )}

        {(turn.model || (turn.usage && (turn.usage.input || turn.usage.output))) && (
          <div className="mt-2 flex items-center gap-2 text-[10px] text-muted">
            {turn.model && <span className="font-mono">{turn.model}</span>}
            {turn.usage && (turn.usage.input || turn.usage.output) && (
              <span>
                {turn.usage.input ?? "?"} in / {turn.usage.output ?? "?"} out tokens
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export function MessageBubble({ turn }: { turn: Turn }) {
  return turn.kind === "user" ? (
    <UserBubble content={turn.content} />
  ) : (
    <AssistantBubble turn={turn} />
  );
}
