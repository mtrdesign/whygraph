import { useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { ApiError, type Note, type NoteAnchorState, type NoteKind, type NoteRetireReason } from "../../api";
import { formatDateTime } from "../../lib/format";
import {
  NOTE_BODY_MAX,
  NOTE_KINDS,
  KIND_LABEL,
  SOURCE_LABEL,
  STATUS_LABEL,
  anchorText,
  chatSessionOf,
  isAgentNote,
  isProjectWide,
  proposalAnchor,
  useInvalidateNotes,
  useViewerUid,
} from "../../lib/notes";
import { useProjectApi, useSlug } from "../../lib/project";
import { plural } from "../../lib/plural";
import { cn } from "../../lib/utils";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Textarea } from "../ui/textarea";
import { ErrorState } from "../state/ErrorState";
import { ConfirmDialog } from "../portal/ConfirmDialog";
import { nativeSelect } from "../portal/Field";

const STATE_BADGE: Record<NoteAnchorState, { label: string; variant: "success" | "warning" | "destructive" | "outline" } | null> = {
  current: { label: "Anchor current", variant: "success" },
  changed: { label: "Anchor changed", variant: "warning" },
  missing: { label: "Anchor missing", variant: "destructive" },
  unknown: { label: "Anchor unknown", variant: "outline" },
  "n/a": null,
};

/** The status and anchor-state badges of a note. */
export function NoteBadges({ note }: { note: Note }) {
  const state = note.status === "confirmed" ? STATE_BADGE[note.anchor_state] : null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Badge variant={note.status === "confirmed" ? "success" : note.status === "proposed" ? "brand" : "outline"}>
        {STATUS_LABEL[note.status]}
      </Badge>
      <Badge variant="secondary">{KIND_LABEL[note.kind]}</Badge>
      {state && (
        <Badge variant={state.variant} data-testid="anchor-state">
          {state.label}
        </Badge>
      )}
    </div>
  );
}

type Mode = null | "confirm" | "still-true" | "revise" | "retire";

/**
 * One note with its actions. Bodies render as text only. Actions need `canAct`
 * (`project.chat`); there is deliberately no confirm-all anywhere.
 */
export function NoteCard({
  note,
  canAct,
  canConfigure,
  rejectCount,
  highlighted = false,
  compact = false,
}: {
  note: Note;
  canAct: boolean;
  canConfigure: boolean;
  /** Proposals waiting from this note's proposer and origin; set to offer "Reject all from this origin". */
  rejectCount?: number;
  highlighted?: boolean;
  compact?: boolean;
}) {
  const api = useProjectApi();
  const slug = useSlug();
  const viewer = useViewerUid();
  const invalidate = useInvalidateNotes();
  const [mode, setMode] = useState<Mode>(null);
  const [body, setBody] = useState(note.body);
  const [kind, setKind] = useState<NoteKind>(note.kind);
  const [lines, setLines] = useState({ start: String(note.anchor.line_start ?? ""), end: String(note.anchor.line_end ?? "") });
  const [reason, setReason] = useState<NoteRetireReason>("outdated");
  const [rejectOpen, setRejectOpen] = useState(false);
  // A wide note the server told us needs a second confirmer (the UI only knows project notes up front).
  const [needsSecond, setNeedsSecond] = useState(false);

  const done = () => {
    setMode(null);
    void invalidate();
  };
  const onError = (e: unknown) => {
    if (e instanceof ApiError && e.code === "note_needs_second_confirmer") setNeedsSecond(true);
    // A stale card refetches so the buttons match the note's real state.
    if (e instanceof ApiError && (e.code === "note_state" || e.code === "note_not_found")) void invalidate();
  };

  const edited = body.trim() !== note.body.trim() || kind !== note.kind;
  const wide = isProjectWide(note) || needsSecond;
  const mine = !!viewer && note.proposed_by === viewer;
  const blockedForMe = wide && mine && !canConfigure;
  // A project note's text is edited at confirm only by a project admin (the server refuses others).
  const canEditText = !isProjectWide(note) || canConfigure;

  const confirm = useMutation({
    mutationFn: () =>
      api.confirmNote(note.id, edited && canEditText ? { body: body.trim(), kind } : {}),
    onSuccess: done,
    onError,
  });
  const stillTrue = useMutation({
    mutationFn: () =>
      api.reconfirmNote(
        note.id,
        note.anchor.kind === "range" && note.anchor_state === "changed"
          ? { line_start: Number(lines.start), line_end: Number(lines.end) }
          : {},
      ),
    onSuccess: done,
    onError,
  });
  const revise = useMutation({
    mutationFn: () =>
      api.proposeNote({ body: body.trim(), kind, anchor: proposalAnchor(note.anchor), supersedes_id: note.id }),
    onSuccess: done,
    onError,
  });
  const retire = useMutation({
    mutationFn: (why: NoteRetireReason) => api.retireNote(note.id, why),
    onSuccess: done,
    onError,
  });
  const rejectAll = useMutation({
    mutationFn: () => api.rejectAgentProposals(note.proposed_by, note.origin),
    onSuccess: () => {
      setRejectOpen(false);
      void invalidate();
    },
  });

  const error = confirm.error ?? stillTrue.error ?? revise.error ?? retire.error;
  const busy = confirm.isPending || stillTrue.isPending || revise.isPending || retire.isPending;
  const session = chatSessionOf(note.origin);
  const agent = isAgentNote(note);
  const rangeChanged = note.anchor.kind === "range" && note.anchor_state === "changed";
  const linesOk =
    !rangeChanged ||
    (Number.isInteger(Number(lines.start)) &&
      Number.isInteger(Number(lines.end)) &&
      Number(lines.start) >= 1 &&
      Number(lines.end) >= Number(lines.start));

  const open = (m: Mode) => {
    confirm.reset();
    stillTrue.reset();
    revise.reset();
    retire.reset();
    setBody(note.body);
    setKind(note.kind);
    setMode(m);
  };

  const needsOther: ReactNode = (
    <p className="text-xs text-muted-foreground" data-testid="needs-second">
      Needs another member or an admin to confirm
    </p>
  );

  const editor = (submit: string, onSubmit: () => void, enabled: boolean, allowText: boolean) => (
    <div className="flex flex-col gap-2" data-testid="note-editor">
      {allowText ? (
        <>
          <Textarea
            aria-label="Note text"
            value={body}
            maxLength={NOTE_BODY_MAX}
            onChange={(e) => setBody(e.target.value)}
          />
          <select
            aria-label="Kind"
            className={nativeSelect("w-40")}
            value={kind}
            onChange={(e) => setKind(e.target.value as NoteKind)}
          >
            {NOTE_KINDS.map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </select>
        </>
      ) : (
        <p className="text-xs text-muted-foreground">
          Only a project admin can edit the text of a note that applies to the whole project.
        </p>
      )}
      <div className="flex gap-2">
        <Button size="sm" disabled={busy || !enabled} onClick={onSubmit}>
          {submit}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setMode(null)}>
          Cancel
        </Button>
      </div>
    </div>
  );

  let actions: ReactNode = null;
  if (canAct && mode === null) {
    if (note.status === "proposed") {
      actions = (
        <div className="flex flex-wrap items-center gap-2">
          {blockedForMe ? needsOther : (
            <Button size="sm" onClick={() => open("confirm")}>
              Confirm
            </Button>
          )}
          <Button size="sm" variant="outline" disabled={busy} onClick={() => retire.mutate("rejected")}>
            Reject
          </Button>
          {agent && rejectCount !== undefined && rejectCount > 0 && (
            <Button size="sm" variant="ghost" onClick={() => setRejectOpen(true)}>
              Reject all from this origin
            </Button>
          )}
        </div>
      );
    } else if (note.status === "confirmed") {
      actions = (
        <div className="flex flex-wrap items-center gap-2">
          {blockedForMe ? needsOther : (
            <Button size="sm" onClick={() => open("still-true")}>
              Still true
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={() => open("revise")}>
            Revise
          </Button>
          <Button size="sm" variant="outline" onClick={() => open("retire")}>
            Retire
          </Button>
        </div>
      );
    }
  } else if (canAct && mode === "confirm") {
    actions = editor("Confirm", () => confirm.mutate(), body.trim().length > 0, canEditText);
  } else if (canAct && mode === "still-true") {
    actions = (
      <div className="flex flex-col gap-2">
        {rangeChanged && (
          <div className="flex flex-wrap items-end gap-2" data-testid="line-picker">
            <p className="w-full text-xs text-muted-foreground">
              The lines changed since this note was confirmed. Pick the lines it applies to now.
            </p>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              From line
              <Input
                type="number"
                min={1}
                className="w-24"
                value={lines.start}
                onChange={(e) => setLines({ ...lines, start: e.target.value })}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              To line
              <Input
                type="number"
                min={1}
                className="w-24"
                value={lines.end}
                onChange={(e) => setLines({ ...lines, end: e.target.value })}
              />
            </label>
          </div>
        )}
        <div className="flex gap-2">
          <Button size="sm" disabled={busy || !linesOk} onClick={() => stillTrue.mutate()}>
            Still true
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setMode(null)}>
            Cancel
          </Button>
        </div>
      </div>
    );
  } else if (canAct && mode === "revise") {
    actions = editor("Propose revision", () => revise.mutate(), body.trim().length > 0 && edited, true);
  } else if (canAct && mode === "retire") {
    actions = (
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Reason
          <select
            className={nativeSelect("w-48")}
            value={reason}
            onChange={(e) => setReason(e.target.value as NoteRetireReason)}
          >
            <option value="outdated">Outdated</option>
            <option value="wrong">Wrong</option>
          </select>
        </label>
        <Button size="sm" variant="destructive" disabled={busy} onClick={() => retire.mutate(reason)}>
          Retire
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setMode(null)}>
          Cancel
        </Button>
      </div>
    );
  }

  return (
    <article
      className={cn("flex flex-col gap-2 rounded-xl bg-card p-4 shadow-card", highlighted && "ring-2 ring-ring")}
      data-testid={`note-${note.id}`}
      aria-label={`Note ${note.id}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <NoteBadges note={note} />
        <span className="text-xs text-muted-foreground">#{note.id}</span>
      </div>
      {/* Text only, never markup. */}
      <p className="whitespace-pre-wrap break-words text-sm text-foreground" data-testid="note-body">
        {note.body}
      </p>
      <div className="break-all font-mono text-xs text-muted-foreground" data-testid="note-anchor">
        {anchorText(note.anchor)}
      </div>
      {note.commits_since ? (
        <p className="text-xs text-muted-foreground">{plural(note.commits_since, "commit")} touched this since it was confirmed.</p>
      ) : null}
      {!compact && (
        <p className="text-xs text-muted-foreground">
          Proposed by {note.proposed_by_name ?? "a member"} {formatDateTime(note.proposed_at)}
          {note.confirmed_at ? ` - confirmed by ${note.confirmed_by_name ?? "a member"} ${formatDateTime(note.confirmed_at)}` : ""}
          {note.retired_at ? ` - retired ${formatDateTime(note.retired_at)}${note.retire_reason ? ` (${note.retire_reason})` : ""}` : ""}
        </p>
      )}
      {agent && (
        <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground" data-testid="note-origin">
          <Badge variant="info">{SOURCE_LABEL[note.source]}</Badge>
          {session !== null && viewer === note.proposed_by ? (
            <Link
              to="/p/$slug/chat/{-$id}"
              params={{ slug, id: String(session) }}
              className="text-primary-text underline-offset-4 hover:underline"
            >
              From chat #{session}
            </Link>
          ) : (
            <span className="font-mono">{note.origin}</span>
          )}
        </p>
      )}
      {note.edited_on_confirm && (
        <p className="text-xs text-muted-foreground" data-testid="edited-on-confirm">
          edited on confirm
        </p>
      )}
      {note.supersedes_id ? <p className="text-xs text-muted-foreground">Revises note #{note.supersedes_id}</p> : null}
      {note.superseded_by_ids && note.superseded_by_ids.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Revised by {note.superseded_by_ids.map((i) => `#${i}`).join(", ")}
        </p>
      )}
      {actions}
      {error != null && <ErrorState size="inline" error={error} />}
      {rejectAll.isError && <ErrorState size="inline" error={rejectAll.error} />}
      <ConfirmDialog
        open={rejectOpen}
        onOpenChange={setRejectOpen}
        title="Reject all from this origin"
        description={`This rejects ${plural(rejectCount ?? 0, "proposal")} from ${note.proposed_by_name ?? "this member"} (${note.origin}). It never confirms anything.`}
        confirmLabel={`Reject ${plural(rejectCount ?? 0, "proposal")}`}
        pending={rejectAll.isPending}
        error={null}
        onConfirm={() => rejectAll.mutate()}
      />
    </article>
  );
}
