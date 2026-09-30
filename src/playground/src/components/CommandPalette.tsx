import { useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { portalApi, portalKey } from "../api";
import { useOpenNode } from "../lib/nav";
import { useProjectQuery } from "../lib/project";
import { useUi } from "../store";
import { useTheme, type Theme } from "../theme";
import { KindBadge, CoverageDot } from "../lib/ui";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "./ui/command";

// The ⌘K menu, available in both scopes. Static entries (pages, projects, theme)
// are filtered here; symbol results come from the server (`api.search`, project
// scope only), so cmdk's own fuzzy filter is off. Selecting a symbol goes through
// the router-backed `openNode()` with the file path, so the tree can auto-reveal
// and the graph recentres.

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

const THEMES: { theme: Theme; label: string }[] = [
  { theme: "system", label: "Theme: System" },
  { theme: "light", label: "Theme: Light" },
  { theme: "dark", label: "Theme: Dark" },
];

function SymbolResults({ query, onDone }: { query: string; onDone: () => void }) {
  const openNode = useOpenNode();
  const { data, isFetching } = useProjectQuery(["search", query], (api) => api.search(query));
  const results = data?.results ?? [];
  return (
    <>
      {results.length > 0 && (
        <CommandGroup heading="Symbols">
          {results.map((r) => (
            <CommandItem
              key={r.id}
              value={r.id}
              onSelect={() => {
                openNode(r.qualified_name, r.file_path);
                onDone();
              }}
              className="gap-2"
            >
              <CoverageDot analyzed={r.analyzed} />
              <KindBadge kind={r.kind} />
              <span className="font-medium">{r.name}</span>
              <span className="truncate text-xs text-muted-foreground">{r.file_path}</span>
            </CommandItem>
          ))}
        </CommandGroup>
      )}
      {isFetching && results.length === 0 && (
        <div className="px-3 py-2 text-xs text-muted-foreground">Searching symbols…</div>
      )}
    </>
  );
}

export function CommandPalette({ slug }: { slug?: string }) {
  const open = useUi((s) => s.paletteOpen);
  const setOpen = useUi((s) => s.setPaletteOpen);
  const navigate = useNavigate();
  const { setTheme } = useTheme();
  const [query, setQuery] = useState("");
  const debounced = useDebounced(query, 150);
  const term = query.trim().toLowerCase();

  const projects = useQuery({
    queryKey: portalKey("projects"),
    queryFn: portalApi.projects,
    enabled: open,
  });

  const close = () => {
    setOpen(false);
    setQuery("");
  };
  const go = (fn: () => void) => () => {
    fn();
    close();
  };
  const match = (label: string) => term === "" || label.toLowerCase().includes(term);

  const pages: { label: string; run: () => void }[] = slug
    ? [
        { label: "Overview", run: () => navigate({ to: "/p/$slug", params: { slug } }) },
        { label: "Explorer", run: () => navigate({ to: "/p/$slug/explorer", params: { slug } }) },
        { label: "Chat", run: () => navigate({ to: "/p/$slug/chat/{-$id}", params: { slug } }) },
        { label: "Scans", run: () => navigate({ to: "/p/$slug/scans/{-$runId}", params: { slug } }) },
        { label: "Project settings", run: () => navigate({ to: "/p/$slug/settings", params: { slug } }) },
        { label: "All projects", run: () => navigate({ to: "/" }) },
      ]
    : [
        { label: "Projects", run: () => navigate({ to: "/" }) },
        { label: "Add project", run: () => navigate({ to: "/projects/new" }) },
        { label: "Settings", run: () => navigate({ to: "/settings" }) },
      ];
  const shownPages = pages.filter((p) => match(p.label));
  const shownProjects = (projects.data?.projects ?? []).filter(
    (p) => p.slug !== slug && match(`Switch to ${p.name}`),
  );
  const shownThemes = THEMES.filter((t) => term !== "" && match(t.label));

  return (
    <CommandDialog
      open={open}
      onOpenChange={(next) => (next ? setOpen(true) : close())}
      title="Command menu"
      description="Go to a page, switch project, change theme or search symbols."
    >
      <Command shouldFilter={false}>
        <CommandInput
          autoFocus
          value={query}
          onValueChange={setQuery}
          placeholder={slug ? "Search symbols, pages, projects…" : "Go to a page or project…"}
        />
        <CommandList>
          <CommandEmpty>No results.</CommandEmpty>
          {shownPages.length > 0 && (
            <CommandGroup heading="Go to">
              {shownPages.map((p) => (
                <CommandItem key={p.label} value={`page:${p.label}`} onSelect={go(p.run)}>
                  {p.label}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {shownProjects.length > 0 && (
            <CommandGroup heading="Projects">
              {shownProjects.map((p) => (
                <CommandItem
                  key={p.slug}
                  value={`project:${p.slug}`}
                  onSelect={go(() => navigate({ to: "/p/$slug", params: { slug: p.slug } }))}
                >
                  Switch to {p.name}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {shownThemes.length > 0 && (
            <CommandGroup heading="Appearance">
              {shownThemes.map((t) => (
                <CommandItem key={t.theme} value={`theme:${t.theme}`} onSelect={go(() => setTheme(t.theme))}>
                  {t.label}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {slug && open && debounced.trim().length > 0 && (
            <SymbolResults query={debounced.trim()} onDone={close} />
          )}
        </CommandList>
      </Command>
    </CommandDialog>
  );
}
