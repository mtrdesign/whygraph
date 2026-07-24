import { useEffect, useState } from "react";
import { Command } from "cmdk";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useExplorer } from "../store";
import { KindBadge, CoverageDot } from "../lib/ui";

// Cmd-K search. Results come from the server (`api.search`), so cmdk's built-in
// fuzzy filtering is disabled. Selecting a row fires the canonical `openNode()`
// with the file path, so the tree can auto-reveal and the graph recentres.

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

export function CommandPalette() {
  const open = useExplorer((s) => s.paletteOpen);
  const setOpen = useExplorer((s) => s.setPaletteOpen);
  const openNode = useExplorer((s) => s.openNode);
  const [query, setQuery] = useState("");
  const debounced = useDebounced(query, 150);

  const { data, isFetching } = useQuery({
    queryKey: ["search", debounced],
    queryFn: () => api.search(debounced),
    enabled: open && debounced.trim().length > 0,
  });

  const results = data?.results ?? [];

  return (
    <Command.Dialog
      open={open}
      onOpenChange={setOpen}
      shouldFilter={false}
      label="Search symbols"
      className="fixed left-1/2 top-24 z-50 w-[560px] max-w-[90vw] -translate-x-1/2 overflow-hidden rounded-xl border border-border bg-panel shadow-2xl"
    >
      <div className="fixed inset-0 -z-10 bg-black/50" onClick={() => setOpen(false)} />
      <Command.Input
        autoFocus
        value={query}
        onValueChange={setQuery}
        placeholder="Search symbols by name…"
        className="w-full border-b border-border bg-transparent px-4 py-3 text-sm text-fg outline-none placeholder:text-muted"
      />
      <Command.List className="max-h-[320px] overflow-auto p-1">
        {debounced.trim().length === 0 && (
          <div className="p-4 text-sm text-muted">Type to search symbols…</div>
        )}
        {debounced.trim().length > 0 && !isFetching && results.length === 0 && (
          <Command.Empty className="p-4 text-sm text-muted">
            No symbols match “{debounced}”.
          </Command.Empty>
        )}
        {results.map((r) => (
          <Command.Item
            key={r.id}
            value={r.id}
            onSelect={() => openNode(r.qualified_name, r.file_path)}
            className="flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm text-fg data-[selected=true]:bg-accent/20"
          >
            <CoverageDot analyzed={r.analyzed} />
            <KindBadge kind={r.kind} />
            <span className="font-medium">{r.name}</span>
            <span className="truncate text-xs text-muted">{r.file_path}</span>
          </Command.Item>
        ))}
      </Command.List>
    </Command.Dialog>
  );
}
