import { Badge } from "./ui/badge";
import { cn } from "@/lib/utils";

// A symbol kind as a small uppercase tag. Tones come from the semantic tokens
// (never raw colours), so every kind stays legible in both themes.
const KIND_TONE: Record<string, string> = {
  class: "bg-primary/15 text-primary-text",
  method: "bg-success/15 text-success",
  function: "bg-success/15 text-success",
  variable: "bg-warning/15 text-warning",
  import: "bg-destructive/15 text-destructive",
};

export function KindBadge({ kind }: { kind: string }) {
  return (
    <Badge
      variant="secondary"
      className={cn(
        "h-4 shrink-0 rounded-sm px-1.5 text-[10px] font-medium uppercase tracking-wide",
        KIND_TONE[kind],
      )}
    >
      {kind}
    </Badge>
  );
}

/** Whether a symbol has a generated rationale card. */
export function CoverageDot({ analyzed }: { analyzed: boolean }) {
  return (
    <span
      title={analyzed ? "Rationale generated" : "Not yet analyzed"}
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        analyzed ? "bg-success" : "bg-muted-foreground/40",
      )}
    />
  );
}
