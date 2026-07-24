import type { NodeRelations, RelationSymbol, Symbol } from "../api";
import { useExplorer } from "../store";
import { KindBadge, EmptyState } from "../lib/ui";

// The Relationships tab: calls / called-by / imports / contained-by / children.
// Every row is a navigation target — clicking it fires the canonical openNode().

function Row({ symbol }: { symbol: RelationSymbol | Symbol }) {
  const openNode = useExplorer((s) => s.openNode);
  return (
    <button
      onClick={() => openNode(symbol.qualified_name, symbol.file_path)}
      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-panel2"
    >
      <KindBadge kind={symbol.kind} />
      <span className="truncate font-medium text-fg">{symbol.name}</span>
      <span className="truncate text-xs text-muted">{symbol.file_path}</span>
    </button>
  );
}

function Section({ title, items }: { title: string; items: (RelationSymbol | Symbol)[] }) {
  if (items.length === 0) return null;
  return (
    <div className="mb-3">
      <div className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
        {title} ({items.length})
      </div>
      {items.map((s, i) => (
        <Row key={`${s.id}-${i}`} symbol={s} />
      ))}
    </div>
  );
}

export function RelationshipsTab({ relations }: { relations: NodeRelations }) {
  const empty =
    relations.callers.length === 0 &&
    relations.callees.length === 0 &&
    relations.imports.length === 0 &&
    relations.children.length === 0 &&
    !relations.container;

  if (empty) return <EmptyState>No relationships recorded for this symbol.</EmptyState>;

  return (
    <div className="p-2">
      {relations.container && <Section title="Contained by" items={[relations.container]} />}
      <Section title="Called by" items={relations.callers} />
      <Section title="Calls" items={relations.callees} />
      <Section title="Imports" items={relations.imports} />
      <Section title="Children" items={relations.children} />
    </div>
  );
}
