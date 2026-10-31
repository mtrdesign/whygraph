import { useInfiniteQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { projectApi, projectKey, type Note } from "../api";
import { NoteCard } from "../components/notes/NoteCard";
import { PageContainer } from "../components/layout/PageContainer";
import { ErrorState } from "../components/state/ErrorState";
import { TableSkeleton } from "../components/state/Skeletons";
import { Button } from "../components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useProjectCan } from "../lib/permissions";
import { isAgentNote } from "../lib/notes";
import { useProjectQuery, useSlug } from "../lib/project";
import { NOTE_FILTERS, type NoteFilter, type NotesSearch } from "../lib/routeSearch";

const PAGE = 50;

const FILTER_LABEL: Record<NoteFilter, string> = {
  review: "Waiting for review",
  confirmed: "Confirmed",
  attention: "Needs attention",
  retired: "Retired",
};

const EMPTY: Record<NoteFilter, string> = {
  review: "Nothing is waiting for review.",
  confirmed: "No confirmed notes yet. Notes are short facts the team confirmed: decisions, constraints, gotchas and conventions.",
  attention: "No confirmed note needs attention.",
  retired: "No retired notes.",
};

function listCall(filter: NoteFilter, before_id: number | undefined) {
  const base = { limit: PAGE, before_id };
  if (filter === "review") return { ...base, status: "proposed" as const };
  if (filter === "confirmed") return { ...base, status: "confirmed" as const };
  if (filter === "retired") return { ...base, status: "retired" as const };
  return { ...base, attention: true };
}

/** `/p/<slug>/notes`: review, confirm and retire the project's notes. */
export function NotesPage() {
  const slug = useSlug();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as NotesSearch;
  const filter: NoteFilter = search.filter ?? "review";
  const canAct = useProjectCan("project.chat");
  const canConfigure = useProjectCan("project.configure");

  const list = useInfiniteQuery({
    queryKey: [...projectKey(slug, "notes", "list"), filter],
    queryFn: ({ pageParam }) => projectApi(slug).notes(listCall(filter, pageParam)),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => (last.length >= PAGE ? last[last.length - 1].id : undefined),
    retry: false,
  });
  const linked = useProjectQuery(["notes", "one", search.id], (api) => api.note(search.id!), {
    enabled: search.id !== undefined,
    retry: false,
  });

  const notes: Note[] = list.data?.pages.flat() ?? [];
  // How many agent proposals wait per proposer and origin, among the rows loaded.
  const waiting = new Map<string, number>();
  if (filter === "review") {
    for (const n of notes) {
      if (n.status === "proposed" && isAgentNote(n)) {
        const key = `${n.proposed_by}\u0000${n.origin}`;
        waiting.set(key, (waiting.get(key) ?? 0) + 1);
      }
    }
  }

  const go = (next: NotesSearch) =>
    void navigate({ to: "/p/$slug/notes", params: { slug }, search: next, replace: true });

  return (
    <PageContainer width="narrow" className="flex flex-col gap-5">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight">Notes</h1>
        <p className="text-[13px] text-muted-foreground">
          Short facts the team confirmed. Everyone who can open this project can see them.
        </p>
      </div>

      {search.id !== undefined && (
        <section className="flex flex-col gap-2" data-testid="linked-note" aria-label="Linked note">
          {linked.isLoading && <TableSkeleton rows={1} cols={1} label="Loading the note" />}
          {linked.isError && <ErrorState error={linked.error} title="Couldn't load that note" size="inline" />}
          {linked.data && (
            <NoteCard note={linked.data} canAct={canAct} canConfigure={canConfigure} highlighted />
          )}
          <Button size="sm" variant="ghost" className="self-start" onClick={() => go({ filter: search.filter })}>
            Show the list
          </Button>
        </section>
      )}

      <Tabs value={filter} onValueChange={(v) => go({ filter: v as NoteFilter })}>
        <TabsList variant="scrollable" aria-label="Notes">
          {NOTE_FILTERS.map((f) => (
            <TabsTrigger key={f} value={f} data-testid={`filter-${f}`}>
              {FILTER_LABEL[f]}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {list.isLoading && <TableSkeleton rows={4} cols={1} label="Loading notes" />}
      {list.isError && (
        <ErrorState error={list.error} title="Couldn't load the notes" onRetry={() => void list.refetch()} />
      )}
      {list.isSuccess && notes.length === 0 && (
        <p className="text-sm text-muted-foreground" data-testid="notes-empty">
          {EMPTY[filter]}
        </p>
      )}
      {notes.length > 0 && (
        <ul className="flex flex-col gap-3" data-testid="notes-list">
          {notes.map((n) => (
            <li key={n.id}>
              <NoteCard
                note={n}
                canAct={canAct}
                canConfigure={canConfigure}
                highlighted={n.id === search.id}
                rejectCount={filter === "review" ? waiting.get(`${n.proposed_by}\u0000${n.origin}`) : undefined}
              />
            </li>
          ))}
        </ul>
      )}
      {list.hasNextPage && (
        <Button variant="outline" className="self-start" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
          Load more
        </Button>
      )}
      {!canAct && notes.length > 0 && (
        <p className="text-xs text-muted-foreground">
          You can read notes. Confirming or changing them needs the Contributor role.
        </p>
      )}
    </PageContainer>
  );
}
