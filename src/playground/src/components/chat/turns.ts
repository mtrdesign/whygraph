import type { ChatMessage } from "../../api";
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

/**
 * Rebuild display turns from a persisted transcript.
 *
 * Tool rows are matched to their announcing assistant row by `tool_call_id`, so
 * a result whose call is missing (shouldn't happen, but a hand-edited DB could)
 * is dropped rather than rendered orphaned.
 */
export function turnsFromMessages(messages: ChatMessage[]): Turn[] {
  const turns: Turn[] = [];
  let current: AssistantTurn | null = null;

  const flush = () => {
    if (current) turns.push(current);
    current = null;
  };

  for (const message of messages) {
    if (message.role === "user") {
      flush();
      turns.push({ kind: "user", content: message.content, id: message.id });
      continue;
    }

    if (message.role === "assistant") {
      // A persisted row is finished by definition — never show it thinking.
      // The first row's id becomes the turn's stable React key.
      if (!current) current = { ...emptyAssistantTurn(), thinking: false, id: message.id };
      // A new assistant row after tool activity opens the next segment.
      const lastGroup = current.activityGroups[current.activityGroups.length - 1];
      if (lastGroup && lastGroup.length > 0) {
        current.segments.push(message.content);
        current.activityGroups.push([]);
      } else {
        current.segments[current.segments.length - 1] = message.content;
      }
      for (const call of message.tool_calls) {
        current.activityGroups[current.activityGroups.length - 1].push({
          id: call.id,
          name: call.name,
          arguments: call.arguments,
          running: false,
        });
      }
      if (message.input_tokens || message.output_tokens) {
        current.usage = {
          input: message.input_tokens,
          output: message.output_tokens,
        };
      }
      // Attribution is per row, so a transcript spanning a mid-session model
      // switch shows each turn's real model rather than the current selection.
      if (message.model) current.model = message.model;
      // A failed turn carries why it failed on its row, so the rose banner
      // survives a refresh instead of dying with the SSE stream.
      if (message.error) current.error = message.error;
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
