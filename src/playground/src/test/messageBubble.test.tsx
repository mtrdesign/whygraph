import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MessageBubble, type AssistantTurn, type UserTurn } from "../components/chat/MessageBubble";

afterEach(cleanup);

function assistant(overrides: Partial<AssistantTurn> = {}): AssistantTurn {
  return { kind: "assistant", segments: [""], activityGroups: [[]], ...overrides };
}

function user(overrides: Partial<UserTurn> = {}): UserTurn {
  return { kind: "user", content: "why is this here?", id: 1, ...overrides };
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

describe("MessageBubble: Regenerate on a reply that said nothing", () => {
  it("offers Regenerate on the no-reply line when the thread allows it", () => {
    const onRegenerate = vi.fn();
    render(<MessageBubble turn={assistant({ id: 2 })} onRegenerate={onRegenerate} />);
    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    expect(onRegenerate).toHaveBeenCalledTimes(1);
  });

  it("offers nothing on an older turn (no handler) or while a turn streams", () => {
    const { rerender } = render(<MessageBubble turn={assistant({ id: 2 })} />);
    expect(screen.queryByRole("button", { name: "Regenerate" })).toBeNull();
    rerender(<MessageBubble turn={assistant({ id: 2 })} onRegenerate={() => {}} busy />);
    expect(screen.queryByRole("button", { name: "Regenerate" })).toBeNull();
  });
});

describe("MessageBubble: how a turn was cut short", () => {
  it("says so when the reply hit the output limit (either spelling)", () => {
    for (const finishReason of ["max_tokens", "length"]) {
      const { unmount } = render(<MessageBubble turn={assistant({ segments: ["Half an ans"], finishReason })} />);
      expect(screen.getByTestId("output-limit-notice")).toHaveTextContent("The reply hit the output limit.");
      expect(screen.queryByTestId("declined-notice")).toBeNull();
      unmount();
    }
  });

  it("says so when the provider declined (either spelling)", () => {
    for (const finishReason of ["refusal", "content_filter"]) {
      const { unmount } = render(<MessageBubble turn={assistant({ segments: ["I can't"], finishReason })} />);
      expect(screen.getByTestId("declined-notice")).toHaveTextContent("The provider declined to answer.");
      expect(screen.queryByTestId("output-limit-notice")).toBeNull();
      unmount();
    }
  });

  it("says nothing for a normal ending", () => {
    render(<MessageBubble turn={assistant({ segments: ["Done."], finishReason: "stop" })} />);
    expect(screen.queryByTestId("output-limit-notice")).toBeNull();
    expect(screen.queryByTestId("declined-notice")).toBeNull();
    expect(screen.queryByTestId("trimmed-notice")).toBeNull();
  });

  it("says when older tool results were shortened", () => {
    render(<MessageBubble turn={assistant({ segments: ["Done."], trimmed: true })} />);
    expect(screen.getByTestId("trimmed-notice")).toHaveTextContent(
      "Older tool results in this turn were shortened to fit the context.",
    );
  });

  it("shows a status note in place of Thinking…", () => {
    render(<MessageBubble turn={assistant({ thinking: true, statusNote: "Stopping the previous reply…" })} />);
    expect(screen.getByText("Stopping the previous reply…")).toBeInTheDocument();
    expect(screen.queryByText("Thinking…")).toBeNull();
  });
});

describe("MessageBubble: Copy", () => {
  const stubClipboard = () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    return writeText;
  };

  it("copies the reply's text segments, not its tool cards or notices", async () => {
    const writeText = stubClipboard();
    render(
      <MessageBubble
        turn={assistant({
          segments: ["First part.", "Second part."],
          activityGroups: [[card], []],
          finishReason: "max_tokens",
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy reply" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("First part.\n\nSecond part."));
  });

  it("copies a question as written", async () => {
    const writeText = stubClipboard();
    render(<MessageBubble turn={user()} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy message" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("why is this here?"));
  });

  it("has nothing to copy on a reply with no text", () => {
    render(<MessageBubble turn={assistant({ activityGroups: [[card]] })} />);
    expect(screen.queryByRole("button", { name: "Copy reply" })).toBeNull();
  });
});

describe("MessageBubble: actions", () => {
  const versions = { index: 2, count: 3, siblings: [5, 9, 14] };

  it("shows Regenerate where the thread passes it, and not where it does not", () => {
    const { unmount } = render(
      <MessageBubble turn={assistant({ segments: ["Latest."], id: 20 })} onRegenerate={() => {}} />,
    );
    expect(screen.getByRole("button", { name: "Regenerate reply" })).toBeInTheDocument();
    unmount();

    // An older turn: the thread passes no handler. (A later plan step turns this on for any turn.)
    render(<MessageBubble turn={assistant({ segments: ["Older."], id: 10 })} />);
    expect(screen.queryByRole("button", { name: "Regenerate reply" })).toBeNull();
    expect(screen.getByRole("button", { name: "Copy reply" })).toBeInTheDocument();
  });

  it("hides every action and arrow while a turn streams", () => {
    render(
      <>
        <MessageBubble
          turn={user({ versions })}
          busy
          onEdit={() => {}}
          onBranch={() => {}}
        />
        <MessageBubble
          turn={assistant({ segments: ["Hi."], versions })}
          busy
          onRegenerate={() => {}}
          onBranch={() => {}}
        />
      </>,
    );
    for (const name of [
      "Copy message",
      "Edit message",
      "Copy reply",
      "Regenerate reply",
      "Previous version",
      "Next version",
    ]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(screen.queryByTestId("version-arrows")).toBeNull();
  });

  it("shows no actions on a live turn that is still thinking", () => {
    render(<MessageBubble turn={assistant({ segments: ["Par"], thinking: true })} onRegenerate={() => {}} />);
    expect(screen.queryByRole("button", { name: "Copy reply" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Regenerate reply" })).toBeNull();
  });

  it("edits a question: Enter saves, Shift+Enter is a newline, Escape cancels, unchanged is a cancel", () => {
    const onEdit = vi.fn();
    render(<MessageBubble turn={user()} onEdit={onEdit} />);

    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    let box = screen.getByRole("textbox", { name: "Edit message" });
    expect(box).toHaveValue("why is this here?");
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(onEdit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    box = screen.getByRole("textbox", { name: "Edit message" });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onEdit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    box = screen.getByRole("textbox", { name: "Edit message" });
    fireEvent.change(box, { target: { value: "why is this *here*?" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onEdit).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onEdit).toHaveBeenCalledWith("why is this *here*?");
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("MessageBubble: version arrows", () => {
  it("show only when a message has more than one version", () => {
    const { rerender } = render(<MessageBubble turn={user()} onBranch={() => {}} />);
    expect(screen.queryByTestId("version-arrows")).toBeNull();

    rerender(
      <MessageBubble turn={user({ versions: { index: 1, count: 1, siblings: [1] } })} onBranch={() => {}} />,
    );
    expect(screen.queryByTestId("version-arrows")).toBeNull();

    rerender(
      <MessageBubble
        turn={user({ versions: { index: 2, count: 3, siblings: [1, 4, 8] } })}
        onBranch={() => {}}
      />,
    );
    expect(screen.getByTestId("version-arrows")).toHaveTextContent("2/3");
  });

  it("step to the neighbouring sibling, and stop at the ends", () => {
    const onBranch = vi.fn();
    const { rerender } = render(
      <MessageBubble
        turn={assistant({ segments: ["B"], versions: { index: 2, count: 3, siblings: [10, 20, 30] } })}
        onBranch={onBranch}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Previous version" }));
    expect(onBranch).toHaveBeenLastCalledWith(10);
    fireEvent.click(screen.getByRole("button", { name: "Next version" }));
    expect(onBranch).toHaveBeenLastCalledWith(30);

    rerender(
      <MessageBubble
        turn={assistant({ segments: ["A"], versions: { index: 1, count: 3, siblings: [10, 20, 30] } })}
        onBranch={onBranch}
      />,
    );
    expect(screen.getByRole("button", { name: "Previous version" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next version" })).toBeEnabled();

    rerender(
      <MessageBubble
        turn={assistant({ segments: ["C"], versions: { index: 3, count: 3, siblings: [10, 20, 30] } })}
        onBranch={onBranch}
      />,
    );
    expect(screen.getByRole("button", { name: "Next version" })).toBeDisabled();
  });
});

describe("MessageBubble: reasoning", () => {
  it("renders reasoning collapsed above its round's text, and Copy leaves it out", async () => {
    window.localStorage.removeItem("whygraph.chat.reasoningOpen");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(
      <MessageBubble
        turn={{
          kind: "assistant",
          segments: ["The answer."],
          activityGroups: [[]],
          reasoning: ["private chain of thought"],
        }}
      />,
    );
    const block = screen.getByTestId("reasoning") as HTMLDetailsElement;
    expect(block.open).toBe(false);
    expect(block).toHaveTextContent("Reasoning");
    fireEvent.click(screen.getByRole("button", { name: "Copy reply" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("The answer."));
  });

  it("remembers that this viewer opened reasoning", () => {
    window.localStorage.setItem("whygraph.chat.reasoningOpen", "1");
    render(
      <MessageBubble
        turn={{ kind: "assistant", segments: ["a"], activityGroups: [[]], reasoning: ["r"] }}
      />,
    );
    expect((screen.getByTestId("reasoning") as HTMLDetailsElement).open).toBe(true);
    window.localStorage.removeItem("whygraph.chat.reasoningOpen");
  });
});
