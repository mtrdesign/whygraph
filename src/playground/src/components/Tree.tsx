import { useEffect, useState } from "react";
import { ChevronRightIcon } from "lucide-react";
import type { TreeEntry } from "../api";
import { useExplorerSearch, useOpenNode } from "../lib/nav";
import { useProjectQuery } from "../lib/project";
import { KindBadge } from "./KindBadge";
import { ErrorState } from "./state/ErrorState";
import { ScrollArea } from "./ui/scroll-area";
import { cn } from "@/lib/utils";

// The left-hand containment tree: dir → file → class → method, lazy-loaded one
// level per expand. Expansion state is lifted to the root so `openNode()` from
// anywhere can auto-reveal the directory path to the selected symbol.

function Chevron({ open }: { open: boolean }) {
  return (
    <ChevronRightIcon
      aria-hidden
      className={cn("size-3 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")}
    />
  );
}

interface LevelProps {
  dir?: string;
  node?: string;
  depth: number;
  expanded: Set<string>;
  onToggle: (id: string) => void;
}

function TreeLevel({ dir, node, depth, expanded, onToggle }: LevelProps) {
  const { data, isLoading, isError, error, refetch } = useProjectQuery(["tree", { dir, node }], (api) =>
    api.tree({ dir, node }),
  );

  if (isLoading)
    return <div style={{ paddingLeft: depth * 14 + 22 }} className="py-1 text-xs text-muted-foreground">…</div>;
  if (isError)
    return (
      <div style={{ paddingLeft: depth * 14 + 22 }} className="py-1 pr-2">
        <ErrorState size="inline" error={error} onRetry={() => void refetch()} className="text-xs" />
      </div>
    );

  const entries = data?.entries ?? [];
  if (entries.length === 0 && depth === 0)
    return (
      <div className="px-3 py-2 text-xs text-muted-foreground">
        Nothing indexed yet. Run a scan from the project Overview to fill the tree.
      </div>
    );
  if (entries.length === 0)
    return (
      <div style={{ paddingLeft: depth * 14 + 22 }} className="py-1 text-xs text-muted-foreground/60">
        (empty)
      </div>
    );

  return (
    <>
      {entries.map((entry) => (
        <TreeRow
          key={entry.id}
          entry={entry}
          depth={depth}
          expanded={expanded}
          onToggle={onToggle}
        />
      ))}
    </>
  );
}

function TreeRow({
  entry,
  depth,
  expanded,
  onToggle,
}: {
  entry: TreeEntry;
  depth: number;
  expanded: Set<string>;
  onToggle: (id: string) => void;
}) {
  const selectedQn = useExplorerSearch().node;
  const openNode = useOpenNode();
  const isOpen = expanded.has(entry.id);
  const isSelected = entry.qualified_name != null && entry.qualified_name === selectedQn;
  const isDir = entry.kind === "directory";

  const handleClick = () => {
    if (entry.qualified_name) {
      openNode(entry.qualified_name, entry.path);
      if (entry.has_children) onToggle(entry.id);
    } else if (entry.has_children) {
      onToggle(entry.id);
    }
  };

  return (
    <>
      <div
        onClick={handleClick}
        style={{ paddingLeft: depth * 14 + 8 }}
        className={cn(
          "flex cursor-pointer items-center gap-1.5 py-1 pr-2 text-sm hover:bg-accent",
          isSelected && "bg-primary-soft text-primary-text",
        )}
      >
        {entry.has_children ? (
          <span onClick={(e) => (e.stopPropagation(), onToggle(entry.id))}>
            <Chevron open={isOpen} />
          </span>
        ) : (
          <span className="inline-block w-3 shrink-0" />
        )}
        <span className="min-w-0 truncate" title={entry.label}>
          {entry.label}
        </span>
        {!isDir && <KindBadge kind={entry.kind} />}
      </div>
      {isOpen && entry.has_children && (
        <TreeLevel
          dir={entry.dir}
          node={entry.node_id}
          depth={depth + 1}
          expanded={expanded}
          onToggle={onToggle}
        />
      )}
    </>
  );
}

export function Tree() {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const { node, file } = useExplorerSearch();
  // BUG-17: `?node=` without `&file=` (a pasted link) resolves the file from the node's own
  // payload (the query the detail panel shares), so the tree still expands to it.
  const resolved = useProjectQuery(["node", node], (api) => api.node(node!), { enabled: !!node && !file });
  const selectedFilePath = file ?? resolved.data?.symbol.file_path;

  const onToggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  // Auto-reveal: expand the directory chain down to the selected symbol's file.
  useEffect(() => {
    if (!selectedFilePath) return;
    const parts = selectedFilePath.split("/");
    setExpanded((prev) => {
      const next = new Set(prev);
      let acc = "";
      for (let i = 0; i < parts.length - 1; i++) {
        acc = acc ? `${acc}/${parts[i]}` : parts[i];
        next.add(`dir:${acc}`);
      }
      return next;
    });
  }, [selectedFilePath]);

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Explorer
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="py-1">
          <TreeLevel depth={0} expanded={expanded} onToggle={onToggle} />
        </div>
      </ScrollArea>
    </div>
  );
}
