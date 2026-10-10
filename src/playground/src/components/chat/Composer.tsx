import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "../ui/button";
import { Textarea } from "../ui/textarea";

/**
 * The message input.
 *
 * Enter sends, Shift-Enter newlines — the convention every chat UI uses. Sending
 * is disabled while a turn streams: the harness holds one threadpool thread per
 * turn, and two concurrent turns on one session would interleave rows.
 */
export function Composer({
  streaming,
  fill,
  disabled,
  notice,
  disabledPlaceholder = "Add a key to start chatting",
  onSend,
  onStop,
}: {
  streaming: boolean;
  /** Text to put in the box (a starter prompt); `n` changes on every request, so the same text can be picked twice. */
  fill?: { text: string; n: number } | null;
  /** Nothing can be sent (no key for the chosen provider): the box is dead and `notice` says why. */
  disabled?: boolean;
  /** The reason under the box; replaces the keyboard hint. */
  notice?: ReactNode;
  /** The box's placeholder while it is disabled (default: no key). */
  disabledPlaceholder?: string;
  onSend: (content: string) => void;
  onStop: () => void;
}) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!fill) return;
    setValue(fill.text);
    ref.current?.focus();
  }, [fill]);

  const submit = () => {
    const content = value.trim();
    if (!content || streaming || disabled) return;
    setValue("");
    onSend(content);
    ref.current?.focus();
  };

  return (
    <div className="border-t border-border bg-sidebar p-3">
      <div className="flex items-end gap-2">
        <Textarea
          ref={ref}
          rows={2}
          className="max-h-40 min-h-0 resize-none"
          value={value}
          disabled={streaming || disabled}
          aria-describedby={disabled && notice ? "composer-notice" : undefined}
          placeholder={
            streaming
              ? "Waiting for the assistant…"
              : disabled
                ? disabledPlaceholder
                : "Ask about this repository…"
          }
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        {streaming ? (
          <Button variant="outline" onClick={onStop} className="shrink-0">
            Stop
          </Button>
        ) : (
          <Button onClick={submit} disabled={disabled || !value.trim()} className="shrink-0">
            Send
          </Button>
        )}
      </div>
      {notice ? (
        <div id="composer-notice" className="mt-1.5 text-xs text-muted-foreground" data-testid="chat-no-key">
          {notice}
        </div>
      ) : (
        <div className="mt-1.5 text-[10px] text-muted-foreground">Enter to send · Shift-Enter for a newline</div>
      )}
    </div>
  );
}
