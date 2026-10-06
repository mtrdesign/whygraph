import { Link } from "@tanstack/react-router";
import type { ProjectSummary } from "../../api";
import { linkNotice, reconnectSearch, safeHref } from "../../lib/platformLink";
import { cn } from "@/lib/utils";

/**
 * A linked project's connection line and, per status, what is wrong and what to
 * do (plan section 4.11). Compact enough for a card; the same text on the home page.
 */
export function LinkNotice({ project }: { project: ProjectSummary }) {
  if (project.source !== "platform") return null;
  const link = project.link ?? null;
  const n = linkNotice(link);
  const manage = safeHref(link?.manage_url);
  return (
    <div
      data-testid="link-notice"
      data-status={link?.status ?? "unknown"}
      className="flex flex-col gap-1 text-xs"
    >
      <span
        className={cn(
          "font-medium",
          n.tone === "ok" && "text-foreground",
          n.tone === "warn" && "text-warning",
          n.tone === "error" && "text-destructive",
        )}
      >
        {n.title}
      </span>
      {n.detail && <span className="text-muted-foreground">{n.detail}</span>}
      {n.actions.length > 0 && (
        <span className="relative z-10 flex flex-wrap gap-x-3 gap-y-1">
          {link && n.actions.includes("reconnect") && (
            <Link to="/link" search={reconnectSearch(link)} className="text-primary-text hover:underline">
              Reconnect
            </Link>
          )}
          {n.actions.includes("remove") && (
            <Link
              to="/p/$slug/settings"
              params={{ slug: project.slug }}
              className="text-primary-text hover:underline"
            >
              Remove from this machine
            </Link>
          )}
          {n.actions.includes("manage") && manage && (
            <a href={manage} target="_blank" rel="noreferrer" className="text-primary-text hover:underline">
              Manage on platform
            </a>
          )}
        </span>
      )}
    </div>
  );
}
