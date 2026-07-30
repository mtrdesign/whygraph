import { useRef, useState } from "react";
import { Button, Textarea } from "../../lib/ui";

/**
 * The message input.
 *
 * Enter sends, Shift-Enter newlines — the convention every chat UI uses. Sending
 * is disabled while a turn streams: the harness holds one threadpool thread per
 * turn, and two concurrent turns on one session would interleave rows.
 */
export function Composer({
  streaming,
  onSend,
  onStop,
}: {
  streaming: boolean;
  onSend: (content: string) => void;
  onStop: () => void;
}) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  const submit = () => {
    const content = value.trim();
    if (!content || streaming) return;
    setValue("");
    onSend(content);
    ref.current?.focus();
  };

  return (
    <div className="border-t border-border bg-panel p-3">
      <div className="flex items-end gap-2">
        <Textarea
          ref={ref}
          rows={2}
          value={value}
          disabled={streaming}
          placeholder={
            streaming ? "Waiting for the assistant…" : "Ask about this repository…"
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
          <Button variant="ghost" onClick={onStop} className="shrink-0">
            Stop
          </Button>
        ) : (
          <Button onClick={submit} disabled={!value.trim()} className="shrink-0">
            Send
          </Button>
        )}
      </div>
      <div className="mt-1.5 text-[10px] text-muted">
        Enter to send · Shift-Enter for a newline
      </div>
    </div>
  );
}
