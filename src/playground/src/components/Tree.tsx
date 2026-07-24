import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { api, type TreeEntry } from "../api";
import { useExplorer } from "../store";
import { KindBadge } from "../lib/ui";

// The left-hand containment tree: dir → file → class → method, lazy-loaded one
// level per expand. Expansion state is lifted to the root so `openNode()` from
// anywhere can auto-reveal the directory path to the selected symbol.

function Chevron({ open }: { open: boolean }) {
  return (
    <span
      className={clsx(
        "inline-block w-3 shrink-0 text-muted transition-transform",
        open && "rotate-90",
      )}
    >
      ▶
    </span>
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
  const { data, isLoading, isError } = useQuery({
    queryKey: ["tree", { dir, node }],
    queryFn: () => api.tree({ dir, node }),
  });

  if (isLoading)
    return <div style={{ paddingLeft: depth * 14 + 22 }} className="py-1 text-xs text-muted">…</div>;
  if (isError)
    return (
      <div style={{ paddingLeft: depth * 14 + 22 }} className="py-1 text-xs text-rose-400">
        failed to load
      </div>
    );

  const entries = data?.entries ?? [];
  if (entries.length === 0)
    return (
      <div style={{ paddingLeft: depth * 14 + 22 }} className="py-1 text-xs text-muted/60">
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
  const selectedQn = useExplorer((s) => s.selectedQn);
  const openNode = useExplorer((s) => s.openNode);
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
        className={clsx(
          "flex cursor-pointer items-center gap-1.5 py-1 pr-2 text-sm hover:bg-panel2",
          isSelected && "bg-accent/20 text-fg",
        )}
      >
        {entry.has_children ? (
          <span onClick={(e) => (e.stopPropagation(), onToggle(entry.id))}>
            <Chevron open={isOpen} />
          </span>
        ) : (
          <span className="inline-block w-3 shrink-0" />
        )}
        <span className="truncate">{entry.label}</span>
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
  const selectedFilePath = useExplorer((s) => s.selectedFilePath);

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
      <div className="border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted">
        Explorer
      </div>
      <div className="flex-1 overflow-auto py-1">
        <TreeLevel depth={0} expanded={expanded} onToggle={onToggle} />
      </div>
    </div>
  );
}
