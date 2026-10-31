import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { ApiError } from "../../api";
import { useActiveSessionId } from "../../lib/nav";
import { KIND_LABEL, STATUS_LABEL, anchorText, isProjectWide, proposedNoteId, useInvalidateNotes, useViewerUid } from "../../lib/notes";
import { useProjectCan } from "../../lib/permissions";
import { useProjectApi, useProjectQuery, useSlug } from "../../lib/project";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { ErrorState } from "../state/ErrorState";

/**
 * The card under a `propose_note` tool call. It is built from `GET /notes/{id}`, never from the
 * streamed result (the stream cuts results at 2,000 characters) or the assistant's text, and it
 * refetches after every action, so a reloaded session shows the note's current status.
 */
export function ProposedNoteCard({ result }: { result: string | undefined }) {
  const noteId = proposedNoteId(result);
  // A spent budget or an error carries no note id: nothing to show.
  if (noteId === null) return null;
  return <Card noteId={noteId} />;
}

function Card({ noteId }: { noteId: number }) {
  const api = useProjectApi();
  const slug = useSlug();
  const session = useActiveSessionId();
  const viewer = useViewerUid();
  const canChat = useProjectCan("project.chat");
  const invalidate = useInvalidateNotes();
  const [needsSecond, setNeedsSecond] = useState(false);

  const query = useProjectQuery(["notes", "one", noteId], (a) => a.note(noteId), { retry: false });
  const act = useMutation({
    mutationFn: (what: "confirm" | "discard") =>
      what === "confirm" ? api.confirmNote(noteId, {}) : api.retireNote(noteId, "rejected"),
    onSuccess: () => void invalidate(),
    onError: (e) => {
      if (e instanceof ApiError && e.code === "note_needs_second_confirmer") setNeedsSecond(true);
      void invalidate();
    },
  });

  if (query.isLoading)
    return (
      <div className="my-1.5 rounded-md border border-border p-3 text-xs text-muted-foreground" data-testid="note-card-loading">
        Loading the proposed note…
      </div>
    );
  if (query.isError || !query.data)
    return (
      <div className="my-1.5">
        <ErrorState size="inline" error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );

  const note = query.data;
  const own = !!viewer && note.proposed_by === viewer && session !== null && note.origin === `chat:${session}`;
  const proposed = note.status === "proposed";
  const wide = isProjectWide(note) || needsSecond;
  const review = (
    <Link
      to="/p/$slug/notes"
      params={{ slug }}
      search={{ id: note.id }}
      className="text-primary-text underline-offset-4 hover:underline"
    >
      Review
    </Link>
  );

  return (
    <div className="my-1.5 flex flex-col gap-2 rounded-md border border-border bg-background p-3 text-sm" data-testid="note-card">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={note.status === "confirmed" ? "success" : proposed ? "brand" : "outline"}>
          {STATUS_LABEL[note.status]}
        </Badge>
        <Badge variant="secondary">{KIND_LABEL[note.kind]}</Badge>
        <span className="text-xs text-muted-foreground">Note #{note.id}</span>
      </div>
      {/* Text only, and the whole body. */}
      <p className="whitespace-pre-wrap break-words" data-testid="note-card-body">
        {note.body}
      </p>
      <div className="break-all font-mono text-xs text-muted-foreground">{anchorText(note.anchor)}</div>
      {wide && (
        <p className="text-xs text-muted-foreground" data-testid="note-card-scope">
          Applies to the whole project: shown at the start of every agent session once confirmed.
        </p>
      )}
      {own && (
        <p className="text-xs text-muted-foreground">The assistant wrote this. Confirm only what you said or know is true.</p>
      )}
      {!own && proposed && (
        <p className="text-xs text-muted-foreground" data-testid="note-card-foreign">
          Already proposed by {note.proposed_by_name ?? "another member"}.
        </p>
      )}
      <p className="text-xs text-muted-foreground">Everyone who can open this project can see it.</p>
      {canChat && (
        <div className="flex flex-wrap items-center gap-2">
          {own && !wide && (
            <Button size="sm" disabled={!proposed || act.isPending} onClick={() => act.mutate("confirm")}>
              Confirm
            </Button>
          )}
          {own && (
            <Button size="sm" variant="outline" disabled={!proposed || act.isPending} onClick={() => act.mutate("discard")}>
              Discard
            </Button>
          )}
          {((own && wide) || (!own && proposed)) && review}
        </div>
      )}
      {act.isError && !needsSecond && <ErrorState size="inline" error={act.error} />}
    </div>
  );
}
