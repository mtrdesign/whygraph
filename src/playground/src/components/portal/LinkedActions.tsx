import { ExternalLinkIcon } from "lucide-react";
import type { ProjectSummary } from "../../api";
import { platformHost, safeHref } from "../../lib/platformLink";
import { Button } from "../ui/button";
import { LinkNotice } from "./LinkNotice";

/**
 * The three platform URLs of a linked project's `link`, as buttons that open in a new tab.
 * Chat is left out for a viewer: viewers have no chat on the platform (M2f-1).
 */
export function PlatformButtons({ project, size }: { project: ProjectSummary; size?: "sm" }) {
  const link = project.link;
  const items = [
    { label: "Manage on platform", href: safeHref(link?.manage_url), primary: true },
    { label: "Open Explorer on platform", href: safeHref(link?.explorer_url) },
    { label: "Open Chat on platform", href: link?.project_role === "viewer" ? undefined : safeHref(link?.chat_url) },
  ].filter((i) => i.href);
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2" data-testid="platform-buttons">
      {items.map((i) => (
        <Button
          key={i.label}
          size={size}
          variant={i.primary ? "default" : "outline"}
          render={<a href={i.href} target="_blank" rel="noreferrer" />}
        >
          {i.label}
          <ExternalLinkIcon data-icon="inline-end" />
        </Button>
      ))}
    </div>
  );
}

/** What a linked project's local `/explorer` and `/chat` show: they live on the platform. */
export function LinkedElsewhere({ project }: { project: ProjectSummary }) {
  const host = project.link ? platformHost(project.link.platform_origin) : "the platform";
  return (
    <section
      data-testid="linked-elsewhere"
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card"
    >
      <h2 className="text-sm font-semibold">The Explorer and Chat are on the platform</h2>
      <p className="text-sm text-muted-foreground">
        {project.name} is linked to a project on {host}, which keeps its history. This machine only
        answers your agent about your own changes, so it has no Explorer or Chat of its own.
      </p>
      <LinkNotice project={project} />
      <PlatformButtons project={project} />
    </section>
  );
}
