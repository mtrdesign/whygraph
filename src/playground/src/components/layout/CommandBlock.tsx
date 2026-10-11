import { Fragment } from "react";
import { cn } from "cn";
import { CopyButton } from "../portal/CopyButton";

/**
 * `text` with a break opportunity after each `/` and `-`, so a long URL or path
 * wraps between its segments rather than leaving one character alone on a line.
 */
export function withSegmentBreaks(text: string) {
  return text.split(/(?<=[/-])/).map((part, i) => (
    <Fragment key={i}>
      {i > 0 && <wbr />}
      {part}
    </Fragment>
  ));
}

/**
 * A shell command on a phone-safe block: it wraps instead of widening the page
 * (at spaces, then after a `/` or `-`; a segment splits only when it alone is
 * wider than the line), and the text shown is exactly the text copied.
 */
export function CommandBlock({ command, className }: { command: string; className?: string }) {
  return (
    <div className={cn("flex items-start gap-2", className)}>
      <pre
        data-scroll-x
        className="min-w-0 flex-1 overflow-x-auto rounded-md bg-muted px-3 py-2 font-mono text-xs wrap-anywhere whitespace-pre-wrap"
      >
        {withSegmentBreaks(command)}
      </pre>
      <div className="shrink-0">
        <CopyButton text={command} />
      </div>
    </div>
  );
}
