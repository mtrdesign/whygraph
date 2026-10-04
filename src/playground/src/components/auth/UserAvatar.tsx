import { useState } from "react";
import { cn } from "@/lib/utils";

/** A GitHub avatar, or the name's initial when there is none (or it fails to load). */
export function UserAvatar({
  name,
  url,
  className,
}: {
  name: string;
  url: string | null | undefined;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  const base = cn("size-7 shrink-0 rounded-full", className);
  if (url && !failed) {
    return (
      <img
        src={url}
        alt=""
        referrerPolicy="no-referrer"
        className={cn(base, "border border-border bg-muted object-cover")}
        onError={() => setFailed(true)}
      />
    );
  }
  return (
    <span
      aria-hidden
      className={cn(base, "flex items-center justify-center bg-primary text-[11px] font-semibold text-primary-foreground")}
    >
      {(name || "?").charAt(0).toUpperCase()}
    </span>
  );
}
