import { useState } from "react";
import { CheckIcon, ChevronRightIcon, CircleAlertIcon, Loader2Icon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * One tool invocation, rendered inline in the thread between text segments.
 *
 * The point of showing these at all is visibility: the user should be able to
 * see exactly what the assistant looked at, not just trust its summary. Collapsed
 * by default (the arguments are usually enough context), expandable to the raw
 * result.
 *
 * `running` matters most for `get_rationale`, which can spend tens of seconds
 * generating a card — without a live card that wait reads as a hung stream.
 */
export interface ToolActivity {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  result?: string;
  running: boolean;
}

function summarize(args: Record<string, unknown>): string {
  const parts = Object.entries(args).map(([key, value]) => {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return `${key}: ${text.length > 40 ? `${text.slice(0, 40)}…` : text}`;
  });
  return parts.join(", ");
}

/** Pretty-print a JSON tool result, falling back to the raw text. */
function formatResult(result: string): string {
  try {
    return JSON.stringify(JSON.parse(result), null, 2);
  } catch {
    return result;
  }
}

export function ToolCallCard({ activity }: { activity: ToolActivity }) {
  const [open, setOpen] = useState(false);
  const { name, arguments: args, result, running } = activity;
  // An `{"error": ...}` payload is worth flagging: the model recovers from these
  // silently, and a user reading the answer should know a lookup missed.
  const failed = !running && !!result && /^\s*\{\s*"error"\s*:/.test(result);

  return (
    <div className="my-1.5 overflow-hidden rounded-md border border-border bg-muted/60 text-xs">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-accent"
      >
        <span
          className={cn(
            "shrink-0",
            running ? "text-primary-text" : failed ? "text-warning" : "text-success",
          )}
          aria-hidden
        >
          {running ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : failed ? (
            <CircleAlertIcon className="size-3.5" />
          ) : (
            <CheckIcon className="size-3.5" />
          )}
        </span>
        <span className="font-mono text-primary-text">{name}</span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">{summarize(args)}</span>
        <ChevronRightIcon
          aria-hidden
          className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")}
        />
      </button>

      {open && (
        <div className="space-y-2 border-t border-border px-2.5 py-2">
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              Arguments
            </div>
            <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-sm bg-background p-2 font-mono text-[11px] text-foreground">
              {JSON.stringify(args, null, 2)}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              Result
            </div>
            {running ? (
              <div className="text-muted-foreground">running…</div>
            ) : (
              // Plain text in a <pre>: tool results are repo content and never
              // get rendered as markup.
              <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded-sm bg-background p-2 font-mono text-[11px] text-foreground">
                {result ? formatResult(result) : "(no result)"}
              </pre>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
