import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import type { NoteAnchor, NoteCheck, NoteKind } from "../../api";
import { KIND_LABEL, NOTE_BODY_MAX, checkProblem, NOTE_KINDS, useInvalidateNotes } from "../../lib/notes";
import { useProjectApi } from "../../lib/project";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
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
  const [checkKind, setCheckKind] = useState<"" | NoteCheck["kind"]>("");
  const [checkText, setCheckText] = useState("");
  const [checkGlob, setCheckGlob] = useState("");
  const [existing, setExisting] = useState<number | null>(null);

  const withCheck = kind === "constraint" && checkKind !== "";
  const problem = withCheck ? checkProblem(checkText, checkGlob) : null;
  const add = useMutation({
    mutationFn: () =>
      api.proposeNote({
        body: body.trim(),
        kind,
        anchor,
        ...(kind === "constraint" && checkKind !== ""
          ? { check: { kind: checkKind, text: checkText.trim(), glob: checkGlob.trim() } }
          : {}),
      }),
    onSuccess: (note) => {
      setExisting(note.duplicate ? note.id : null);
      setBody("");
      setCheckKind("");
      setCheckText("");
      setCheckGlob("");
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
      {kind === "constraint" && (
        <div className="flex flex-col gap-2" data-testid="check-row">
          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="Check"
              className={nativeSelect("w-48")}
              value={checkKind}
              onChange={(e) => setCheckKind(e.target.value as "" | NoteCheck["kind"])}
            >
              <option value="">No check</option>
              <option value="absent">Must not appear</option>
              <option value="present">Must appear</option>
            </select>
            {checkKind !== "" && (
              <>
                <Input
                  aria-label="Check text"
                  className="w-56"
                  placeholder="Text to look for"
                  maxLength={200}
                  value={checkText}
                  onChange={(e) => setCheckText(e.target.value)}
                />
                <Input
                  aria-label="Check glob"
                  className="w-56"
                  placeholder="Files, for example src/**/*.py"
                  maxLength={200}
                  value={checkGlob}
                  onChange={(e) => setCheckGlob(e.target.value)}
                />
              </>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            A check is a text search in git-tracked files; it never runs a command. It starts once the note is confirmed.
          </p>
          {problem && checkText !== "" && checkGlob !== "" && (
            <p className="text-xs text-destructive" data-testid="check-hint">
              {problem}
            </p>
          )}
        </div>
      )}
      <p className="text-xs text-muted-foreground">Everyone who can open this project can see it.</p>
      <div className="flex gap-2">
        <Button size="sm" type="submit" disabled={add.isPending || body.trim().length === 0 || (withCheck && problem !== null)}>
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
