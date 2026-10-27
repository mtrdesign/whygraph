import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../api";
import type { AssistantTurn } from "../components/chat/MessageBubble";
import { turnsFromMessages } from "../components/chat/turns";
import baseline from "./chatTurns.baseline.json";

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

  it("takes finishReason from the turn's last assistant row", () => {
    const turns = turnsFromMessages([
      row({ role: "user", content: "q" }),
      row({ content: "a", tool_calls: [call("c1")], finish_reason: "tool_use" }),
      row({ role: "tool", content: "{}", tool_call_id: "c1" }),
      row({ content: "b", finish_reason: "max_tokens" }),
    ]);
    expect((turns[1] as AssistantTurn).finishReason).toBe("max_tokens");

    // The first row said "output limit", the last one ended cleanly: nothing to warn about.
    const ended = turnsFromMessages([
      row({ role: "user", content: "q" }),
      row({ content: "a", finish_reason: "max_tokens" }),
      row({ content: "b", finish_reason: "stop" }),
    ]);
    expect((ended[1] as AssistantTurn).finishReason).toBe("stop");

    const none = turnsFromMessages([row({ role: "user", content: "q" }), row({ content: "a" })]);
    expect((none[1] as AssistantTurn).finishReason).toBeUndefined();
  });

  it("renders a merged continuation (two assistant rows, no tool row between) as one turn", () => {
    const turns = turnsFromMessages([
      row({ role: "user", content: "q" }),
      row({ content: "The first half, ", finish_reason: "max_tokens", input_tokens: 10, output_tokens: 5 }),
      row({ content: "and the second.", finish_reason: "stop", input_tokens: 12, output_tokens: 6 }),
    ]);
    expect(turns).toHaveLength(2);
    const assistant = turns[1] as AssistantTurn;
    expect(assistant.segments).toEqual(["The first half, and the second."]);
    expect(assistant.usage).toEqual({ input: 22, output: 11 });
    expect(assistant.finishReason).toBe("stop");
  });

  it("looks versions up by the turn's FIRST row id", () => {
    const user = row({ role: "user", content: "q" });
    const first = row({ content: "", tool_calls: [call("c1")] });
    const tool = row({ role: "tool", content: "{}", tool_call_id: "c1" });
    const last = row({ content: "done" });
    const userVersions = { index: 2, count: 3, siblings: [user.id - 10, user.id, user.id + 10] };
    const replyVersions = { index: 1, count: 2, siblings: [first.id, first.id + 20] };
    const turns = turnsFromMessages([user, first, tool, last], {
      [String(user.id)]: userVersions,
      [String(first.id)]: replyVersions,
      // Keyed by a later row of the turn: ignored.
      [String(last.id)]: { index: 1, count: 9, siblings: [last.id] },
    });
    expect(turns[0]).toMatchObject({ kind: "user", versions: userVersions });
    expect((turns[1] as AssistantTurn).versions).toEqual(replyVersions);
  });

  it("leaves versions off turns that have none", () => {
    const turns = turnsFromMessages([row({ role: "user", content: "q" }), row({ content: "a" })], {});
    expect("versions" in turns[0]).toBe(false);
    expect("versions" in turns[1]).toBe(false);
  });

  // §9.51: a session with no branches renders exactly as before. The expected output was captured from
  // `turnsFromMessages` BEFORE the version / finish-reason change (chatTurns.baseline.json).
  it("renders a transcript with no branches exactly as it did before versions", () => {
    const base = {
      tool_calls: [],
      tool_call_id: null,
      input_tokens: null,
      output_tokens: null,
      provider: null,
      model: null,
      error: null,
      created_at: "2026-10-07T00:00:00Z",
    };
    const fixture: ChatMessage[] = [
      { ...base, id: 1, role: "user", content: "why?" },
      { ...base, id: 2, role: "assistant", content: "Looking. ", tool_calls: [call("c1")], input_tokens: 100, output_tokens: 10, provider: "openai", model: "gpt-x" },
      { ...base, id: 3, role: "tool", content: "{}", tool_call_id: "c1" },
      { ...base, id: 4, role: "assistant", content: "", tool_calls: [call("c2")], input_tokens: 5, output_tokens: 1, model: "gpt-x" },
      { ...base, id: 5, role: "tool", content: "r2", tool_call_id: "c2" },
      { ...base, id: 6, role: "assistant", content: "Found it.", input_tokens: 150, output_tokens: 20, model: "gpt-y" },
      { ...base, id: 7, role: "user", content: "and then?" },
      { ...base, id: 8, role: "assistant", content: "partial", error: "provider_error" },
      { ...base, id: 9, role: "user", content: "again" },
      { ...base, id: 10, role: "assistant", content: "ok", input_tokens: 3, output_tokens: 4 },
      { ...base, id: 11, role: "assistant", content: "This chat has reached its monthly budget.", error: "budget_exceeded" },
      { ...base, id: 12, role: "user", content: "last" },
      { ...base, id: 13, role: "assistant", content: "" },
    ];
    // Strict equality on the serialized form: `toEqual` would let a new `undefined` key through.
    const plain = (value: unknown) => JSON.parse(JSON.stringify(value));
    expect(plain(turnsFromMessages(fixture))).toEqual(baseline);
    expect(plain(turnsFromMessages(fixture, {}))).toEqual(baseline);
  });
});
