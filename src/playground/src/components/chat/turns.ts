import type { ChatMessage } from "../../api";
import type { AssistantTurn, Turn } from "./MessageBubble";

// Persisted rows and live SSE frames describe the same thing in two shapes, and
// both have to render identically — otherwise a reloaded session looks different
// from the one the user just watched. So both funnel into the same `Turn[]`.
//
// The row shape per assistant turn is: assistant(+tool_calls) → tool rows →
// assistant(…) → … → assistant. The display shape is one bubble whose text
// segments and tool-card groups alternate.

export function emptyAssistantTurn(): AssistantTurn {
  return { kind: "assistant", segments: [""], activityGroups: [[]] };
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
      turns.push({ kind: "user", content: message.content });
      continue;
    }

    if (message.role === "assistant") {
      if (!current) current = emptyAssistantTurn();
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
  return { ...turn, segments, activityGroups: groups };
}

/** Add a running tool card to the turn's newest group. */
export function applyToolCall(
  turn: AssistantTurn,
  call: { id: string; name: string; arguments: Record<string, unknown> },
): AssistantTurn {
  const groups = turn.activityGroups.map((g) => [...g]);
  groups[groups.length - 1].push({ ...call, running: true });
  return { ...turn, activityGroups: groups };
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
  return { ...turn, activityGroups: groups };
}

/** Clear any still-running cards — used when a turn ends or is aborted. */
export function settleActivities(turn: AssistantTurn): AssistantTurn {
  return {
    ...turn,
    activityGroups: turn.activityGroups.map((group) =>
      group.map((a) => (a.running ? { ...a, running: false } : a)),
    ),
  };
}
