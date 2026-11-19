import { usePortalState } from "../../lib/identity";
import { alertVariants } from "../ui/alert";
import { cn } from "@/lib/utils";

/**
 * The strip itself: an `Alert` whose text is a text node (never markup), with no
 * close button (plan 59, D44). Also the Configuration page's preview.
 */
export function AnnouncementStrip({
  text,
  level,
  testId = "announcement-banner",
}: {
  text: string;
  level: "info" | "warning";
  testId?: string;
}) {
  return (
    <div
      role="status"
      data-testid={testId}
      data-tone={level}
      className={cn(
        alertVariants({ variant: level }),
        "shrink-0 rounded-none border-x-0 border-t-0 px-4 py-1.5 text-center text-[13px] whitespace-pre-line",
      )}
    >
      {text}
    </div>
  );
}

/**
 * The platform's announcement (production; `announcement` of `GET /api/portal/state`,
 * public, so signed out too). Rendered in the org app's banner stack, both branches of
 * the base chrome, the admin shell and the admin sign-in.
 */
export function AnnouncementBanner() {
  const announcement = usePortalState().data?.announcement;
  if (!announcement?.text) return null;
  return <AnnouncementStrip text={announcement.text} level={announcement.level} />;
}
