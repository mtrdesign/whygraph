import type { EvidenceItem } from "../api";
import { EmptyState } from "../lib/ui";

// Shared renderer for the evidence bundle used by the Evidence and History tabs.
// All fields are untrusted repo content (commit messages, PR/issue titles); React
// escapes text by default and we never use dangerouslySetInnerHTML (§6).

function ExternalLink({ href, children }: { href: string | null; children: string }) {
  if (!href) return <span className="text-muted">{children}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="text-accent2 hover:underline"
    >
      {children}
    </a>
  );
}

function EvidenceCard({ item }: { item: EvidenceItem }) {
  const c = item.commit;
  return (
    <div className="rounded-lg border border-border bg-panel2 p-3">
      <div className="flex items-center gap-2">
        <span className="rounded bg-panel px-1.5 py-0.5 font-mono text-[10px] text-muted">
          {c.sha.slice(0, 8)}
        </span>
        <span className="rounded border border-border px-1.5 py-0.5 text-[10px] uppercase text-muted">
          {item.source}
        </span>
      </div>
      <div className="mt-1.5 text-sm font-medium text-fg">{c.subject}</div>
      {c.llm_description && (
        <div className="mt-1 text-xs text-muted">{c.llm_description}</div>
      )}
      <div className="mt-1 text-[11px] text-muted">
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
    </div>
  );
}

export function EvidenceList({ items, empty }: { items: EvidenceItem[]; empty: string }) {
  if (items.length === 0) return <EmptyState>{empty}</EmptyState>;
  return (
    <div className="space-y-2 p-3">
      {items.map((item) => (
        <EvidenceCard key={item.commit.sha} item={item} />
      ))}
    </div>
  );
}
