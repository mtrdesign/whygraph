import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../api";
import type { AssistantTurn } from "../components/chat/MessageBubble";
import { turnsFromMessages } from "../components/chat/turns";

let nextId = 1;
const row = (overrides: Partial<ChatMessage>): ChatMessage => ({
  id: nextId++,
  role: "assistant",
  content: "",
  tool_calls: [],
  tool_call_id: null,
  input_tokens: null,
  output_tokens: null,
  provider: null,
  model: null,
  error: null,
  created_at: "2026-10-07T00:00:00Z",
  ...overrides,
});
const call = (id: string) => ({ id, name: "search_symbols", arguments: { query: id } });

describe("turnsFromMessages", () => {
  it("sums the tokens of a turn's rounds, as the live done frame does", () => {
    const turns = turnsFromMessages([
      row({ role: "user", content: "why?" }),
      row({ content: "Looking. ", tool_calls: [call("c1")], input_tokens: 100, output_tokens: 10 }),
      row({ role: "tool", content: "{}", tool_call_id: "c1" }),
      row({ content: "Found it.", input_tokens: 150, output_tokens: 20 }),
    ]);
    const assistant = turns[1] as AssistantTurn;
    expect(assistant.usage).toEqual({ input: 250, output: 30 });
  });

  it("keeps a side null only when no round reported it", () => {
    const turns = turnsFromMessages([
      row({ role: "user", content: "q" }),
      row({ content: "a", tool_calls: [call("c1")], input_tokens: 7, output_tokens: null }),
      row({ role: "tool", content: "{}", tool_call_id: "c1" }),
      row({ content: "b", input_tokens: 5, output_tokens: null }),
    ]);
    expect((turns[1] as AssistantTurn).usage).toEqual({ input: 12, output: null });
  });

  it("does not carry tokens across turns", () => {
    const turns = turnsFromMessages([
      row({ role: "user", content: "q1" }),
      row({ content: "a1", input_tokens: 10, output_tokens: 1 }),
      row({ role: "user", content: "q2" }),
      row({ content: "a2", input_tokens: 20, output_tokens: 2 }),
    ]);
    expect((turns[1] as AssistantTurn).usage).toEqual({ input: 10, output: 1 });
    expect((turns[3] as AssistantTurn).usage).toEqual({ input: 20, output: 2 });
  });

  it("renders consecutive tool-only rounds as one card group, like the live stream", () => {
    const turns = turnsFromMessages([
      row({ role: "user", content: "q" }),
      row({ content: "", tool_calls: [call("c1")], input_tokens: 1, output_tokens: 1 }),
      row({ role: "tool", content: "r1", tool_call_id: "c1" }),
      row({ content: "", tool_calls: [call("c2")], input_tokens: 2, output_tokens: 2 }),
      row({ role: "tool", content: "r2", tool_call_id: "c2" }),
      row({ content: "Done.", input_tokens: 3, output_tokens: 3 }),
    ]);
    const assistant = turns[1] as AssistantTurn;
    expect(assistant.segments).toEqual(["", "Done."]);
    expect(assistant.activityGroups.map((g) => g.map((a) => [a.id, a.result]))).toEqual([
      [
        ["c1", "r1"],
        ["c2", "r2"],
      ],
      [],
    ]);
    expect(assistant.usage).toEqual({ input: 6, output: 6 });
  });

  it("shows the latest round's served model", () => {
    const turns = turnsFromMessages([
      row({ role: "user", content: "q" }),
      row({ content: "", tool_calls: [call("c1")], model: "anthropic/claude-opus-5" }),
      row({ role: "tool", content: "{}", tool_call_id: "c1" }),
      row({ content: "ok", model: "anthropic/claude-sonnet-5" }),
    ]);
    expect((turns[1] as AssistantTurn).model).toBe("anthropic/claude-sonnet-5");
  });
});
