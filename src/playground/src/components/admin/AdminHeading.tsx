import type { ReactNode } from "react";

/** The `h1` every admin page renders (`RouteFocus` focuses it), with an optional line under it. */
export function AdminHeading({ children, description }: { children: ReactNode; description?: ReactNode }) {
  return (
    <div>
      <h1 className="text-[22px] font-semibold tracking-tight">{children}</h1>
      {description && <p className="text-[13px] text-muted-foreground">{description}</p>}
    </div>
  );
}
