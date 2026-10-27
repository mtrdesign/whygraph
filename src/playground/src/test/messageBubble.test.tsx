import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MessageBubble, type AssistantTurn } from "../components/chat/MessageBubble";

afterEach(cleanup);

function assistant(overrides: Partial<AssistantTurn> = {}): AssistantTurn {
  return { kind: "assistant", segments: [""], activityGroups: [[]], ...overrides };
}

const card = { id: "c1", name: "search_symbols", arguments: { query: "x" }, result: "{}", running: false };

describe("MessageBubble: how a turn without text ends", () => {
  it("shows the no-reply line, not Thinking…, for settled tool cards with no text", () => {
    render(<MessageBubble turn={assistant({ activityGroups: [[card]], thinking: false })} />);
    expect(screen.getByTestId("no-reply")).toHaveTextContent("The model ended its turn without a reply.");
    expect(screen.queryByText("Thinking…")).toBeNull();
  });

  it("shows the no-reply line for a settled turn with nothing at all", () => {
    render(<MessageBubble turn={assistant()} />);
    expect(screen.getByTestId("no-reply")).toBeInTheDocument();
    expect(screen.queryByText("Thinking…")).toBeNull();
  });

  it("shows Thinking… only while the turn is live", () => {
    render(<MessageBubble turn={assistant({ thinking: true })} />);
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
    expect(screen.queryByTestId("no-reply")).toBeNull();
  });

  it("shows neither while a tool is running", () => {
    render(<MessageBubble turn={assistant({ activityGroups: [[{ ...card, result: undefined, running: true }]] })} />);
    expect(screen.queryByText("Thinking…")).toBeNull();
    expect(screen.queryByTestId("no-reply")).toBeNull();
  });

  it("an error is its own ending, not a missing reply", () => {
    render(<MessageBubble turn={assistant({ error: "Stopped." })} />);
    expect(screen.queryByTestId("no-reply")).toBeNull();
  });

  it("a turn with text has a reply", () => {
    render(<MessageBubble turn={assistant({ segments: ["Because history."] })} />);
    expect(screen.queryByTestId("no-reply")).toBeNull();
  });
});
