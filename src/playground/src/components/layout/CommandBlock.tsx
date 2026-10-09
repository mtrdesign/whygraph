import { cn } from "cn";
import { CopyButton } from "../portal/CopyButton";

/**
 * A shell command on a phone-safe block: it wraps instead of widening the page,
 * and the text shown is exactly the text copied.
 */
export function CommandBlock({ command, className }: { command: string; className?: string }) {
  return (
    <div className={cn("flex items-start gap-2", className)}>
      <pre
        data-scroll-x
        className="min-w-0 flex-1 overflow-x-auto rounded-md bg-muted px-3 py-2 font-mono text-xs break-all whitespace-pre-wrap"
      >
        {command}
      </pre>
      <div className="shrink-0">
        <CopyButton text={command} />
      </div>
    </div>
  );
}
