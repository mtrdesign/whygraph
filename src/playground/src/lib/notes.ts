import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import type { Note, NoteAnchor, NoteKind, NoteStatus } from "../api";
import { usePortalState } from "./identity";
import { useSlug } from "./project";
import { projectKey } from "../api";

export const NOTE_KINDS: NoteKind[] = ["decision", "constraint", "gotcha", "convention"];

/** The most characters a note body may have (`BODY_MAX_CHARS` in `mcp/notes.py`). */
export const NOTE_BODY_MAX = 1000;

export const KIND_LABEL: Record<NoteKind, string> = {
  decision: "Decision",
  constraint: "Constraint",
  gotcha: "Gotcha",
  convention: "Convention",
};

export const STATUS_LABEL: Record<NoteStatus, string> = {
  proposed: "Waiting for review",
  confirmed: "Confirmed",
  retired: "Retired",
};

/** The viewer's uid, the value `proposed_by` holds for their own proposals. */
export function useViewerUid(): string | undefined {
  return usePortalState().data?.user?.uid;
}

/** Refetch every notes query of this project (lists, summary, one note, per-symbol). */
export function useInvalidateNotes(): () => Promise<void> {
  const slug = useSlug();
  const client = useQueryClient();
  return useCallback(() => client.invalidateQueries({ queryKey: projectKey(slug, "notes") }), [client, slug]);
}

/** One line naming what a note is attached to. */
export function anchorText(anchor: NoteAnchor): string {
  switch (anchor.kind) {
    case "project":
      return "The whole project";
    case "area":
      return `Area ${anchor.area ?? ""}`;
    case "path":
      return anchor.path ?? "";
    case "symbol":
      return anchor.qualified_name ?? anchor.path ?? "";
    case "range":
      return `${anchor.path ?? ""}:${anchor.line_start ?? "?"}-${anchor.line_end ?? "?"}`;
  }
}

/**
 * Whether a note surely applies to nearly every session: a project note. The server also treats a
 * path or area anchor on the repo root's direct children (or broader) as wide, but that needs the
 * file tree and the configured areas, so the UI learns it from `note_needs_second_confirmer`.
 */
export function isProjectWide(note: Pick<Note, "anchor">): boolean {
  return note.anchor.kind === "project";
}

/** The chat session a `chat:<id>` origin names, else `null`. */
export function chatSessionOf(origin: string): number | null {
  const m = /^chat:(\d+)$/.exec(origin);
  return m ? Number(m[1]) : null;
}

/** An agent proposal: the Chat assistant or an MCP client wrote it. */
export function isAgentNote(note: Pick<Note, "source">): boolean {
  return note.source === "chat" || note.source === "mcp";
}

export const SOURCE_LABEL: Record<Note["source"], string> = {
  human: "A member",
  chat: "Chat assistant",
  mcp: "Agent over MCP",
};

/** The anchor to send when revising `anchor`: only the fields that identify it. */
export function proposalAnchor(anchor: NoteAnchor): NoteAnchor {
  switch (anchor.kind) {
    case "project":
      return { kind: "project" };
    case "area":
      return { kind: "area", area: anchor.area };
    case "path":
      return { kind: "path", path: anchor.path };
    case "symbol":
      return { kind: "symbol", qualified_name: anchor.qualified_name };
    case "range":
      return { kind: "range", path: anchor.path, line_start: anchor.line_start, line_end: anchor.line_end };
  }
}

/** The `note_id` a `propose_note` tool result starts with, else `null` (a spent budget or an error has none). */
export function proposedNoteId(result: string | undefined): number | null {
  if (!result) return null;
  const m = /^\{"note_id":\s*(\d+)/.exec(result.slice(0, 100));
  return m ? Number(m[1]) : null;
}
