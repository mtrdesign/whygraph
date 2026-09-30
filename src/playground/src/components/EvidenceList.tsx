import type { EvidenceItem } from "../api";
import { Badge } from "./ui/badge";
import { Card } from "./ui/card";
import { Empty, EmptyDescription } from "./ui/empty";

// Shared renderer for the evidence bundle used by the Evidence and History tabs.
// All fields are untrusted repo content (commit messages, PR/issue titles); React
// escapes text by default and we never use dangerouslySetInnerHTML (§6).

function ExternalLink({ href, children }: { href: string | null; children: string }) {
  if (!href) return <span className="text-muted-foreground">{children}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="text-primary-text hover:underline"
    >
      {children}
    </a>
  );
}

function EvidenceCard({ item }: { item: EvidenceItem }) {
  const c = item.commit;
  return (
    <Card size="sm" className="gap-0 bg-muted px-3">
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="h-4 rounded-sm bg-background px-1.5 font-mono text-[10px]">
          {c.sha.slice(0, 8)}
        </Badge>
        <Badge variant="outline" className="h-4 rounded-sm px-1.5 text-[10px] uppercase text-muted-foreground">
          {item.source}
        </Badge>
      </div>
      <div className="mt-1.5 text-sm font-medium text-foreground">{c.subject}</div>
      {c.llm_description && (
        <div className="mt-1 text-xs text-muted-foreground">{c.llm_description}</div>
      )}
      <div className="mt-1 text-[11px] text-muted-foreground">
        {c.author_name} · {c.authored_at}
      </div>
      {(item.pull_requests.length > 0 || item.issues.length > 0) && (
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
          {item.pull_requests.map((pr) => (
            <ExternalLink key={`pr-${pr.number}`} href={pr.html_url}>
              {`#${pr.number} ${pr.title}`}
            </ExternalLink>
          ))}
          {item.issues.map((issue) => (
            <ExternalLink key={`issue-${issue.number}`} href={issue.html_url}>
              {`issue #${issue.number} ${issue.title}`}
            </ExternalLink>
          ))}
        </div>
      )}
    </Card>
  );
}

export function EvidenceList({ items, empty }: { items: EvidenceItem[]; empty: string }) {
  if (items.length === 0)
    return (
      <Empty className="p-4">
        <EmptyDescription>{empty}</EmptyDescription>
      </Empty>
    );
  return (
    <div className="space-y-2 p-3">
      {items.map((item) => (
        <EvidenceCard key={item.commit.sha} item={item} />
      ))}
    </div>
  );
}
