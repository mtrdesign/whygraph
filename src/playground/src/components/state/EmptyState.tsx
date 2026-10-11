import type { ReactNode } from "react";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "../ui/empty";

/**
 * Nothing to show yet. The rule (M2f-3 ER-9): `description` names the next step,
 * not the symptom ("Run a scan to fill this list", not "No runs").
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  secondary,
  className,
}: {
  icon?: ReactNode;
  title: string;
  description: ReactNode;
  /** The primary next step (a button or a link). */
  action?: ReactNode;
  /** A second, quieter one. */
  secondary?: ReactNode;
  className?: string;
}) {
  return (
    <Empty className={className} data-testid="empty-state">
      <EmptyHeader>
        {icon && <EmptyMedia variant="icon">{icon}</EmptyMedia>}
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
      </EmptyHeader>
      {(action || secondary) && (
        <EmptyContent>
          <div className="flex flex-wrap items-center justify-center gap-2">
            {action}
            {secondary}
          </div>
        </EmptyContent>
      )}
    </Empty>
  );
}
