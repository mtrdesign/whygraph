import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import type { NoteAnchor, NoteKind } from "../../api";
import { KIND_LABEL, NOTE_BODY_MAX, NOTE_KINDS, useInvalidateNotes } from "../../lib/notes";
import { useProjectApi } from "../../lib/project";
import { Button } from "../ui/button";
import { Textarea } from "../ui/textarea";
import { ErrorState } from "../state/ErrorState";
import { nativeSelect } from "../portal/Field";

/** Proposes a note on `anchor`. A human proposal still waits for a confirm (its own or another member's). */
export function AddNoteForm({ anchor }: { anchor: NoteAnchor }) {
  const api = useProjectApi();
  const invalidate = useInvalidateNotes();
  const [body, setBody] = useState("");
  const [kind, setKind] = useState<NoteKind>("decision");
  const [open, setOpen] = useState(false);
  const [existing, setExisting] = useState<number | null>(null);

  const add = useMutation({
    mutationFn: () => api.proposeNote({ body: body.trim(), kind, anchor }),
    onSuccess: (note) => {
      setExisting(note.duplicate ? note.id : null);
      setBody("");
      setOpen(false);
      void invalidate();
    },
  });

  if (!open) {
    return (
      <div className="flex flex-col gap-2">
        <Button size="sm" variant="outline" className="self-start" onClick={() => setOpen(true)}>
          Add note
        </Button>
        {existing !== null && (
          <p className="text-xs text-muted-foreground" data-testid="note-duplicate">
            That note already exists as note #{existing}.
          </p>
        )}
      </div>
    );
  }
  return (
    <form
      className="flex flex-col gap-2"
      data-testid="add-note-form"
      onSubmit={(e) => {
        e.preventDefault();
        add.mutate();
      }}
    >
      <Textarea
        aria-label="Note text"
        placeholder="A decision, constraint, gotcha or convention the code does not show"
        value={body}
        maxLength={NOTE_BODY_MAX}
        onChange={(e) => setBody(e.target.value)}
      />
      <select aria-label="Kind" className={nativeSelect("w-40")} value={kind} onChange={(e) => setKind(e.target.value as NoteKind)}>
        {NOTE_KINDS.map((k) => (
          <option key={k} value={k}>
            {KIND_LABEL[k]}
          </option>
        ))}
      </select>
      <p className="text-xs text-muted-foreground">Everyone who can open this project can see it.</p>
      <div className="flex gap-2">
        <Button size="sm" type="submit" disabled={add.isPending || body.trim().length === 0}>
          Propose note
        </Button>
        <Button size="sm" type="button" variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
      {add.isError && <ErrorState size="inline" error={add.error} />}
    </form>
  );
}
