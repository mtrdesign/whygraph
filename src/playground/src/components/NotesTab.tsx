import { useProjectCan } from "../lib/permissions";
import { useProjectQuery } from "../lib/project";
import { Loading } from "./Loading";
import { AddNoteForm } from "./notes/AddNoteForm";
import { NoteCard } from "./notes/NoteCard";
import { ErrorState } from "./state/ErrorState";

// The Notes tab: every note that applies to the symbol, proposed ones and ones whose anchor is
// missing included, so a member can review in place. Modelled on the Rationale tab, but a plain
// read: no LLM call is ever made here.
export function NotesTab({ qualifiedName }: { qualifiedName: string }) {
  const canAct = useProjectCan("project.chat");
  const canConfigure = useProjectCan("project.configure");
  const { data, isLoading, isError, error, refetch } = useProjectQuery(["notes", "target", qualifiedName], (api) =>
    api.notesForTarget(qualifiedName),
  );

  if (isLoading)
    return (
      <div className="p-4">
        <Loading label="Loading notes…" />
      </div>
    );
  if (isError)
    return <ErrorState className="p-3" title="Couldn't load the notes" error={error} onRetry={() => void refetch()} />;

  return (
    <div className="flex flex-col gap-3 p-4" data-testid="notes-tab">
      {data && data.length > 0 ? (
        data.map((note) => <NoteCard key={note.id} note={note} canAct={canAct} canConfigure={canConfigure} compact />)
      ) : (
        <p className="text-sm text-muted-foreground" data-testid="notes-empty">
          No notes apply to this symbol yet.
        </p>
      )}
      {canAct && <AddNoteForm anchor={{ kind: "symbol", qualified_name: qualifiedName }} />}
    </div>
  );
}
