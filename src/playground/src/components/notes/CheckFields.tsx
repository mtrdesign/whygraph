import type { NoteCheck } from "../../api";
import { checkProblem } from "../../lib/notes";
import { Input } from "../ui/input";
import { nativeSelect } from "../portal/Field";

/** The editable state of a note's check; `kind: ""` means no check. */
export interface CheckDraft {
  kind: "" | NoteCheck["kind"];
  text: string;
  glob: string;
}

export const NO_CHECK: CheckDraft = { kind: "", text: "", glob: "" };

/** A draft that starts from a note's existing check, or from none. */
export function draftOf(check: NoteCheck | null | undefined): CheckDraft {
  return check ? { kind: check.kind, text: check.text, glob: check.glob } : NO_CHECK;
}

/** The check a draft stands for, or null for "No check". */
export function checkOf(d: CheckDraft): NoteCheck | null {
  return d.kind === "" ? null : { kind: d.kind, text: d.text.trim(), glob: d.glob.trim() };
}

/** The problem with a draft that has a check, or null. */
export function draftProblem(d: CheckDraft): string | null {
  return d.kind === "" ? null : checkProblem(d.text, d.glob);
}

/** The "Check" row of a constraint: kind select, text and glob inputs, and the hint. */
export function CheckFields({ value, onChange }: { value: CheckDraft; onChange: (next: CheckDraft) => void }) {
  const problem = draftProblem(value);
  return (
    <div className="flex flex-col gap-2" data-testid="check-row">
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Check"
          className={nativeSelect("w-48")}
          value={value.kind}
          onChange={(e) => onChange({ ...value, kind: e.target.value as CheckDraft["kind"] })}
        >
          <option value="">No check</option>
          <option value="absent">Must not appear</option>
          <option value="present">Must appear</option>
        </select>
        {value.kind !== "" && (
          <>
            <Input
              aria-label="Check text"
              className="w-56"
              placeholder="Text to look for"
              maxLength={200}
              value={value.text}
              onChange={(e) => onChange({ ...value, text: e.target.value })}
            />
            <Input
              aria-label="Check glob"
              className="w-56"
              placeholder="Files, for example src/**/*.py"
              maxLength={200}
              value={value.glob}
              onChange={(e) => onChange({ ...value, glob: e.target.value })}
            />
          </>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        A check is a text search in git-tracked files; it never runs a command. It starts once the note is confirmed.
      </p>
      {problem && value.text !== "" && value.glob !== "" && (
        <p className="text-xs text-destructive" data-testid="check-hint">
          {problem}
        </p>
      )}
    </div>
  );
}
