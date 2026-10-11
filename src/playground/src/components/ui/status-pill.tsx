import { cn } from "cn"
import { Badge } from "./badge"

export type StatusPillTone = "ok" | "warn" | "error" | "info" | "busy" | "idle"

const VARIANT = {
  ok: "success",
  warn: "warning",
  error: "destructive",
  info: "info",
  busy: "brand",
  idle: "secondary",
} as const

const DOT: Record<StatusPillTone, string> = {
  ok: "bg-success",
  warn: "bg-warning",
  error: "bg-destructive",
  info: "bg-info",
  busy: "bg-primary-text",
  idle: "bg-muted-foreground/60",
}

/**
 * A soft status badge with a 6 px dot. `busy` (in progress) is the indigo-soft
 * `brand` tone; `pulse` animates the dot for work that is happening right now.
 */
function StatusPill({
  tone,
  label,
  pulse = false,
  className,
  ...props
}: {
  tone: StatusPillTone
  label: string
  pulse?: boolean
} & Omit<React.ComponentProps<typeof Badge>, "variant" | "children">) {
  return (
    <Badge
      variant={VARIANT[tone]}
      data-tone={tone}
      className={cn("gap-1.5", className)}
      {...props}
    >
      <span
        aria-hidden
        className={cn("size-1.5 shrink-0 rounded-full", DOT[tone], pulse && "animate-pulse")}
      />
      {label}
    </Badge>
  )
}

export { StatusPill }
