import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import type { Note, NoteAnchor, NoteCheck, NoteKind, NoteStatus } from "../api";
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
  purpose: "Project purpose",
};

/** One line for a check, for example `must not appear: "foo" in src/**`. */
export function checkText(check: NoteCheck): string {
  return `${check.kind === "absent" ? "must not appear" : "must appear"}: "${check.text}" in ${check.glob}`;
}

/** Client-side hints for a check; the server stays the authority. Returns a message or null. */
export function checkProblem(text: string, glob: string): string | null {
  const t = text.trim();
  const g = glob.trim();
  if (t.length < 3 || t.length > 200) return "The text must be 3 to 200 characters.";
  if (/["`\\\n\r]/.test(t)) return 'The text must be one line without ", ` or \\.';
  if (g.length < 1 || g.length > 200) return "The glob must be 1 to 200 characters.";
  if (!/^[A-Za-z0-9._\-/*]+$/.test(g)) return "The glob may use letters, digits, . _ - / and *.";
  if (g.startsWith("/") || g.split("/").includes("..")) return "The glob must be relative and not use '..'.";
  if ((g.match(/\*\*/g) ?? []).length > 2) return "The glob may use ** at most twice.";
  return null;
}

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
  analysis: "Drafted by analysis",
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
