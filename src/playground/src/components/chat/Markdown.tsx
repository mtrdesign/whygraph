import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AnchorHTMLAttributes } from "react";
import { useExplorer } from "../../store";

// Renders assistant markdown. react-markdown builds React elements rather than
// injecting HTML, so there is no `dangerouslySetInnerHTML` anywhere on the LLM
// output path — the same rule EvidenceList follows for repo content. Raw HTML in
// the model's output is simply not rendered (no rehype-raw), which is the point.
//
// No syntax highlighter in Phase 1 (decided): code blocks are mono-styled <pre>.
// `rehype-highlight` is the designated follow-up.

const SYMBOL_SCHEME = "whygraph://symbol/";

/**
 * A symbol deep-link rendered as a clickable chip.
 *
 * The system prompt tells the model to link symbols as
 * `[name](whygraph://symbol/<qualified_name>)`, so answers can hand the user
 * straight into the Explorer's graph view instead of making them re-search.
 */
function SymbolChip({ qualifiedName, label }: { qualifiedName: string; label: string }) {
  const openNode = useExplorer((s) => s.openNode);
  return (
    <button
      type="button"
      onClick={() => openNode(qualifiedName)}
      title={`Open ${qualifiedName} in the Explorer`}
      className="inline-flex items-center gap-1 rounded border border-accent/40 bg-accent/15 px-1.5 py-0.5 font-mono text-[11px] text-accent2 transition-colors hover:bg-accent/25"
    >
      <span aria-hidden>◆</span>
      {label}
    </button>
  );
}

function Anchor({ href, children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  if (href?.startsWith(SYMBOL_SCHEME)) {
    const qualifiedName = decodeURIComponent(href.slice(SYMBOL_SCHEME.length));
    // The link text is the model's label; fall back to the name itself when the
    // markdown had none.
    const label =
      typeof children === "string" && children ? children : qualifiedName;
    return <SymbolChip qualifiedName={qualifiedName} label={label} />;
  }
  return (
    <a {...rest} href={href} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  );
}

export function Markdown({ children }: { children: string }) {
  return (
    <div
      className={
        "prose prose-invert max-w-none text-sm " +
        // Tighten the typography plugin's generous defaults and bind its colors
        // to our tokens.
        "prose-p:my-2 prose-headings:mb-2 prose-headings:mt-3 prose-headings:text-fg " +
        "prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5 " +
        "prose-a:text-accent2 prose-strong:text-fg " +
        "prose-code:rounded prose-code:bg-panel2 prose-code:px-1 prose-code:py-0.5 " +
        "prose-code:font-mono prose-code:text-[12px] prose-code:before:content-none " +
        "prose-code:after:content-none " +
        "prose-pre:my-2 prose-pre:overflow-x-auto prose-pre:rounded-md " +
        "prose-pre:border prose-pre:border-border prose-pre:bg-panel2 " +
        "prose-pre:p-3 prose-pre:font-mono prose-pre:text-[12px] " +
        "prose-table:text-xs prose-th:text-fg"
      }
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: Anchor }}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
