import type { ChatMessage, ChatVersions } from "../../api";
import type { AssistantTurn, Turn } from "./MessageBubble";

// Persisted rows and live SSE frames describe the same thing in two shapes, and
// both have to render identically — otherwise a reloaded session looks different
// from the one the user just watched. So both funnel into the same `Turn[]`.
//
// The row shape per assistant turn is: assistant(+tool_calls) → tool rows →
// assistant(…) → … → assistant. The display shape is one bubble whose text
// segments and tool-card groups alternate.

// `thinking` is set here rather than on the first frame: the gap it covers
// starts at click time (the server builds the system prompt from live repo
// facts before the first token), so anything stream-driven would be too late.
export function emptyAssistantTurn(): AssistantTurn {
  return { kind: "assistant", segments: [""], activityGroups: [[]], thinking: true };
}

/** Sum two token counts; `null` only when neither side reported one. */
function addTokens(a: number | null | undefined, b: number | null): number | null {
  if (a == null) return b;
  if (b == null) return a;
  return a + b;
}

/**
 * Rebuild display turns from a persisted transcript.
 *
 * Tool rows are matched to their announcing assistant row by `tool_call_id`, so
 * a result whose call is missing (shouldn't happen, but a hand-edited DB could)
 * is dropped rather than rendered orphaned.
 *
 * `messages` is the transcript's active path. `versions` (the transcript's, keyed
 * by message id) is looked up by a turn's FIRST row, so `‹ 2/3 ›` sits on the user
 * question or on the reply's opening row. A reply cut by the output limit and
 * continued is two consecutive assistant rows with no tool row between them: the
 * text-joining below already renders them as one turn. `finishReason` is the
 * turn's last assistant row's.
 */
export function turnsFromMessages(
  messages: ChatMessage[],
  versions?: Record<string, ChatVersions>,
): Turn[] {
  const turns: Turn[] = [];
  let current: AssistantTurn | null = null;

  const flush = () => {
    if (current) turns.push(current);
    current = null;
  };

  for (const message of messages) {
    if (message.role === "user") {
      flush();
      const userVersions = versions?.[String(message.id)];
      turns.push({
        kind: "user",
        content: message.content,
        id: message.id,
        ...(userVersions && { versions: userVersions }),
      });
      continue;
    }

    if (message.role === "assistant") {
      // A persisted row is finished by definition — never show it thinking.
      // The first row's id becomes the turn's stable React key.
      if (!current) {
        current = { ...emptyAssistantTurn(), thinking: false, id: message.id };
        const replyVersions = versions?.[String(message.id)];
        if (replyVersions) current.versions = replyVersions;
      }
      // The row a budget hard stop wrote (its content is the notice's line): a notice, not text.
      if (message.error === "budget_exceeded") {
        current.budgetStop = { message: message.content || undefined };
        continue;
      }
      // A new assistant row after tool activity opens the next segment - but
      // only when it brings text. Each provider round is its own row, so a
      // tool-only round (no text) keeps adding cards to the open group, the
      // same way the live stream renders it.
      const lastGroup = current.activityGroups[current.activityGroups.length - 1];
      if (lastGroup && lastGroup.length > 0) {
        // Reasoning opens the round too, so it shows after the cards it follows.
        if (message.content || message.reasoning) {
          current.segments.push(message.content);
          current.activityGroups.push([]);
        }
      } else {
        current.segments[current.segments.length - 1] += message.content;
      }
      if (message.reasoning) {
        current.reasoning = addReasoning(current.reasoning, current.segments.length - 1, message.reasoning);
      }
      for (const call of message.tool_calls) {
        current.activityGroups[current.activityGroups.length - 1].push({
          id: call.id,
          name: call.name,
          arguments: call.arguments,
          running: false,
        });
      }
      // One row per provider round, so the turn's usage is the sum of its
      // rows - the same total the live `done` frame showed.
      if (message.input_tokens || message.output_tokens) {
        current.usage = {
          input: addTokens(current.usage?.input, message.input_tokens),
          output: addTokens(current.usage?.output, message.output_tokens),
        };
      }
      // Attribution is per row, so a transcript spanning a mid-session model
      // switch shows each turn's real model rather than the current selection.
      if (message.model) current.model = message.model;
      // A failed turn carries why it failed on its row, so the rose banner
      // survives a refresh instead of dying with the SSE stream.
      if (message.error) current.error = message.error;
      // The last assistant row decides how the turn ended (a continued reply's first row said
      // "output limit" and its last row said "stop").
      if (message.finish_reason) current.finishReason = message.finish_reason;
      else delete current.finishReason;
      continue;
    }

    // role === "tool": attach the result to its already-rendered card.
    if (current) {
      for (const group of current.activityGroups) {
        const activity = group.find((a) => a.id === message.tool_call_id);
        if (activity) {
          activity.result = message.content;
          break;
        }
      }
    }
  }

  flush();
  return turns;
}

/** Put `text` at segment `index` of a turn's reasoning (rounds that share a segment join). */
function addReasoning(reasoning: string[] | undefined, index: number, text: string): string[] {
  const next = [...(reasoning ?? [])];
  while (next.length <= index) next.push("");
  next[index] = next[index] ? `${next[index]}\n\n${text}` : text;
  return next;
}

/**
 * Append a reasoning delta. Like text, reasoning after a completed tool group opens the next
 * segment, so it sits above the round's own text rather than above the previous round's.
 */
export function applyReasoningDelta(turn: AssistantTurn, text: string): AssistantTurn {
  const segments = [...turn.segments];
  const groups = turn.activityGroups.map((g) => [...g]);
  const lastGroup = groups[groups.length - 1];
  if (lastGroup && lastGroup.length > 0 && lastGroup.every((a) => !a.running)) {
    segments.push("");
    groups.push([]);
  }
  const index = segments.length - 1;
  const reasoning = [...(turn.reasoning ?? [])];
  while (reasoning.length <= index) reasoning.push("");
  reasoning[index] += text;
  return { ...turn, segments, activityGroups: groups, reasoning };
}

/** Append a text delta to the turn's newest segment. */
export function applyTextDelta(turn: AssistantTurn, text: string): AssistantTurn {
  const segments = [...turn.segments];
  const groups = turn.activityGroups.map((g) => [...g]);
  // Text after a completed tool group starts a new segment, mirroring the
  // server's own round boundary.
  const lastGroup = groups[groups.length - 1];
  if (lastGroup && lastGroup.length > 0 && lastGroup.every((a) => !a.running)) {
    segments.push(text);
    groups.push([]);
  } else {
    segments[segments.length - 1] = (segments[segments.length - 1] ?? "") + text;
  }
  // Tokens are on screen — the gap is over.
  return { ...turn, thinking: false, segments, activityGroups: groups };
}

/** Add a running tool card to the turn's newest group. */
export function applyToolCall(
  turn: AssistantTurn,
  call: { id: string; name: string; arguments: Record<string, unknown> },
): AssistantTurn {
  const groups = turn.activityGroups.map((g) => [...g]);
  groups[groups.length - 1].push({ ...call, running: true });
  // The card's own running spinner takes over from the indicator.
  return { ...turn, thinking: false, activityGroups: groups };
}

/** Resolve a running card with its result. */
export function applyToolResult(
  turn: AssistantTurn,
  id: string,
  result: string,
): AssistantTurn {
  const groups = turn.activityGroups.map((group) =>
    group.map((a) => (a.id === id ? { ...a, result, running: false } : a)),
  );
  // Back to waiting on the model for the next round.
  return { ...turn, thinking: true, activityGroups: groups };
}

/** Clear any still-running cards — used when a turn ends or is aborted. */
export function settleActivities(turn: AssistantTurn): AssistantTurn {
  return {
    ...turn,
    // Terminal in every path that calls this (done / error / abort / a stream
    // that died without a frame), so the indicator can never stick.
    thinking: false,
    activityGroups: turn.activityGroups.map((group) =>
      group.map((a) => (a.running ? { ...a, running: false } : a)),
    ),
  };
}
