# Project notes

A **note** is a short fact about your project that the code does not show: a decision, a constraint, a
gotcha or a convention. "We never call the billing API synchronously from `checkout`." "The importer
must not retry: the vendor bills per call." Rationale cards explain why code exists from its history;
notes hold what the team knows and never wrote down in a commit.

Notes are tied to an **anchor**, written by people (or proposed by agents), and shown to the Chat
assistant and to MCP agents when they look at the code the note is about.

!!! note "Agents propose, people confirm"
    The Chat assistant and MCP agents can **propose** a note. A proposal does nothing until a member
    confirms it, and no model ever reads a proposal. Only confirmed notes reach a model.

## What a note holds

| Part | What it is |
|---|---|
| **Body** | Plain text, at most 1,000 characters. Written by a member; every WhyGraph surface treats it as data about the project, never as an instruction to the assistant. |
| **Kind** | `decision`, `constraint`, `gotcha` or `convention` - or `purpose`, which only the [project purpose](#project-purpose) card makes. |
| **Anchor** | What it is about - see below. |
| **Refs** | Up to five commits or `#N` pull requests and issues that back the note. Each must exist in the project's history. |

### Anchors

A note has exactly one anchor:

| Anchor | Ties the note to | Notes it is shown with |
|---|---|---|
| **Symbol** | A function, class or method, by its qualified name | That symbol's rationale card and lookups |
| **Line range** | Lines of one file | Anything that overlaps those lines |
| **Path** | A file or a folder | Every symbol under it |
| **Area** | One of the project's configured [areas](../reference/configuration.md#areas) | Every file in the area |
| **Project** | The whole project | Every session: the assistant is told that project-wide notes exist and fetches them |

A symbol that is defined in several files cannot be an anchor by name; anchor the file or the lines
instead. The repository root and WhyGraph's own folders (`.git`, `.whygraph`, `.codegraph`,
`whygraph.toml`, `.env*`) cannot be anchors.

### Whether a note is still about the code

A symbol or line-range anchor remembers a fingerprint of the lines it was confirmed on. On every read
WhyGraph compares it with the code, and each note shows one of these states:

| State | Meaning |
|---|---|
| `current` | The anchored lines are unchanged since the note was confirmed (or marked still true). |
| `changed` | The lines changed. The note is still shown, with a warning that the code moved on. |
| `missing` | The symbol, file, path or area is gone. The note leaves the agent surfaces and stays in the Notes page. |
| `unknown` | The index is behind the code, or the note could not be checked (a symbol in several files, too many files in one call). Rescan to settle it. |

Project notes show `n/a`. Path and area notes show how many default-branch commits touched the anchor
since the note was confirmed (`99+` at most) instead of a fingerprint. Looking at a note never starts a
scan.

## Life of a note

```text
proposed --Confirm--> confirmed --Retire (outdated / wrong)--> retired
    \--Discard (rejected)--> retired
```

1. **Propose.** A member adds a note in the UI, or the Chat assistant or an MCP agent proposes one.
   Proposing the same text on the same anchor twice returns the existing note instead of a duplicate.
2. **Confirm.** A member reviews the proposal and confirms it, optionally editing the text or kind first
   (a note edited on confirmation says **edited on confirm**; the original text is kept for the audit
   record and is never shown to a model). Confirming checks the anchor against the code again, so a stale
   index refuses with "rescan, then confirm".
3. **Still true.** A confirmed note whose code changed can be marked **Still true**, which records the
   new fingerprint and returns it to `current`. For a **line-range** note whose lines changed, you choose
   the lines again; the old range is not carried over.
4. **Revise.** A confirmed note is never edited. To change it, propose a revision: a new proposal that
   names the note it replaces. Confirming the revision retires the old one as superseded.
5. **Retire.** A proposal is discarded (rejected). A confirmed note is retired as **outdated** (once
   true, no longer) or **wrong**. Retired notes stay in the Notes page under **Retired**.

### Who can do what

| Role | Can |
|---|---|
| Viewer | Read notes, including proposals. |
| Contributor, project admin, and the single local user | Add a note, confirm, mark still true, revise and retire. |

A note is visible to **everyone who can open the project**, proposals included. That is different from
Chat sessions, which are private to their owner: do not put a secret in a note, and do not ask the
assistant to remember one.

### Notes that reach every session

A **wide** note - a `project` anchor, or a path or area anchor on a top-level folder of the repository or
something broader (`src`, `src/**`, `*`) - reaches nearly every session. So it needs a second pair of
eyes: the member who proposed it cannot confirm it or mark it still true, and nobody but a project
admin can change its text while confirming. Another contributor, or a project admin (on a local portal,
the single local user), can. A contributor who wants other text proposes a revision. A wide note you
proposed shows "Needs another member or an admin to confirm".

## Project purpose { #project-purpose }

A **purpose** is a special note (kind `purpose`, anchored to the whole project) that says in four short
lines what the project is: **What it is**, **Who it is for**, **Main parts** and **Stack**, at most
600 characters in all. It is drafted by an analysis, not written by a person, and it follows the same
rules as any other note: it is a proposal until it is confirmed, and no model reads it before that.

- **How it is made.** A project admin presses **Generate** on the Overview's purpose card, or ticks the
  optional checkbox in the setup wizard (see [Projects](../portal/projects.md#the-first-scan-and-the-purpose)).
  One call to the project's `[analyze]` model reads the README, a few documents, the package manifests
  reduced to names, the top-level file and folder names, the configured area names and a few history
  counts. All of it is read from the git object store at `HEAD`, never from the working tree. The
  answer is checked and rebuilt into the four lines before it is stored. What is sent, and what is
  never read, is in [Security](../portal/security.md#project-purpose).
- **One proposed, one confirmed.** A project has at most one proposed and one confirmed purpose.
  Generating again replaces the proposal; if a purpose is already confirmed, the new one is a revision
  of it, and confirming the revision retires the old one. Retiring a confirmed purpose also discards a
  pending revision. The card shows the sources next to the text, and **Regenerate** stays disabled
  until the inputs have changed since the last run.
- **Who confirms.** The admin who generated a purpose may confirm it alone: the wide-note rule that a
  proposer cannot confirm their own note does not apply here, because the card shows the sources and the
  text together and confirming is an explicit click. A contributor can also confirm an admin's
  **unedited** proposal. Only a project admin can edit the text; an edit by anyone else needs another
  confirmer (`note_needs_second_confirmer`).
- **Agents cannot write one.** `whygraph_propose_note` and the Chat `propose_note` tool refuse the
  `purpose` kind and the `analysis` source.
- **Where it shows up.** Once confirmed, the purpose is a project-wide note: the Chat assistant
  reads it with `find_notes` (see [Chat](chat.md#team-notes)), and on a local portal it also reaches
  MCP agents and the Claude Code session hook. On a production portal it reaches Chat only. It is not available on a [linked project](../portal/platform-projects.md).

## Where you work with notes

- **The Explorer.** Select a symbol: its **Notes** tab lists the notes that apply, proposals and
  missing ones included, so you can review in place, with an **Add note** form anchored to the symbol.
  See [The Explorer](playground.md#notes).
- **The Notes page.** The project's **Notes** item in the sidebar, with a badge for proposals waiting.
  Four filters: **Waiting for review** (agent proposals are marked with their source and, for a Chat
  proposal, a link to the session if it is yours), **Confirmed**, **Needs attention** (confirmed notes
  whose anchor `changed` or went `missing`) and **Retired**. Each note offers Confirm, Still true,
  Revise and Retire. A link such as `/p/<slug>/notes?id=12` opens one note.
- **The Overview.** A **Notes** card with the counts: waiting for review (and how many came from
  agents), and confirmed. See [The Overview](../portal/projects.md#notes-card).
- **Chat.** The assistant looks notes up with `find_notes`, cites them as `note #12`, and proposes
  with `propose_note`. See [Team notes in Chat](chat.md#team-notes).
- **Your editor.** An MCP agent sees confirmed notes in the `notes` of a rationale card, can read one at
  `whygraph://note/<id>`, and can propose with `whygraph_propose_note`; the `/whygraph-note` command
  wraps that. See [Using WhyGraph](mcp-usage.md#notes) and [Connecting agents](../portal/agents.md).

## A check on a constraint { #a-check-on-a-constraint }

A note of kind **Constraint** can carry one **check**: a text search that says whether the constraint
still holds in the code. It is optional, and you set it when you add the note or when you confirm it.

- **Must not appear** (`absent`): the text must be found nowhere in the files the glob names.
- **Must appear** (`present`): the text must be found in at least one of them.

The check has three parts: the kind, the text and a glob, for example `must not appear: "eval(" in
src/**/*.py`. The rules:

| Part | Rule |
|---|---|
| Text | 3 to 200 characters, on one line, with no `"`, backtick or backslash. Matched exactly, with case, as plain text (not a regular expression). |
| Glob | 1 to 200 characters of letters, digits, `.`, `_`, `-`, `/` and `*`. It is relative: no leading `/`, no `..` part, and at most two `**`. |

The portal refuses a check that breaks a rule with `note_check_invalid`. When you confirm a note,
leaving the check out keeps it, and sending `null` removes it. A check counts only on a **confirmed**
note: a proposal's check does nothing until a person confirms it.

### Where a check runs

- **In the portal, for one note.** A single note (and the notes of one symbol or path) shows a tag:
  `holds`, `broken`, or `unknown` when the check could not be run. The portal searches git-tracked
  files only, up to 500 of them. The Notes list shows the check but no tag, because it would search on
  every row.
- **In the plan tool, at plan time and after each step.** `/whygraph-plan` copies each check into the
  plan under **Constraint checks**, and `/whygraph-implement` searches for each text with `git grep`
  after every step (`plan-tool.sh constraints`). It searches tracked **and untracked** files, so a
  new file the step just created counts here even though the portal would not see it yet. Ignored and
  binary files never count.

### When a check breaks

In Claude Code a broken check sends the step to the constraint rung of the recovery ladder: one more
attempt with the broken line as a finding, and if it breaks again the run stops with `a recorded
constraint is at risk`. In Cursor, VS Code and Codex there is no ladder, so a broken check fails the
step's Verify.

If the plan is meant to change the constraint, the step's **Changes on purpose** line names the note
as `(note #<id>)`, and the check is skipped for that step and the ones after it (see
[Planning a change](planning.md#where-a-constraint-comes-from)). Afterwards the run proposes a
revision or a new decision, which a person confirms.

A check is a text search and nothing more: it never runs a command. See
[Project notes](../portal/security.md#project-notes).

## Limits on agent proposals

Agents cannot flood the review queue. Each of these is a `note_limit` refusal, and none of them applies
to a note a person adds:

| Limit | Value |
|---|---|
| Proposals waiting for review from agents, per project | 200 |
| Agent proposals per member per day (UTC) | 20 |
| Agent proposals per project per day (UTC) | 50 |
| Proposals per Chat turn | 2 (a third is answered "the proposal budget for this turn is used", which is not an error) |
| Proposals a `/whygraph-implement` run makes for changes on purpose | 3 |

A `/whygraph-implement` run in Claude Code or Cursor makes one kind of proposal on its own: after its
Acceptance passed, for each step whose **Changes on purpose** line changed a recorded constraint (see
[Planning a change](planning.md#where-a-constraint-comes-from)). When the step changed a note, the
proposal is a revision of that note; otherwise it is a `decision` note that says the old constraint
no longer holds. At most three per run, and the limits above still apply. A person confirms each one
in the portal like any other proposal, and confirming a revision retires the note it replaces. In
VS Code and Codex the run only prints a line per step and proposes nothing.

If an agent floods the queue, **Reject all from this origin** on any of its rows discards every pending
proposal from that member's agent in that one origin (one Chat session, or their MCP client) and
confirms nothing. It leaves other members' proposals and every confirmed note alone.

## What a model gets to see

- Only **confirmed** notes, and `missing` ones are left out.
- At most five notes for one target, the most specific anchor first (a symbol or line range, then a
  path, then an area), newest first.
- Project-wide notes: the Chat overview carries only a **count** ("call `find_notes(project_wide=true)`"),
  never a body, and neither does the MCP repository overview. The assistant asks for the bodies.
- A note whose anchor `changed` is shown with that state, and the assistant says so when it cites it.
- Notes never feed the generation of a rationale card, and a cached card is not invalidated by a note.

## Imported notes { #imported-notes }

A project admin can [import a bundle](../portal/transfer.md) from another portal or project of the same
repository. Imported notes arrive with the status they had (confirmed stays confirmed, and the preview
shows every wide confirmed note in full before you apply), and show a small **imported** badge beside
the author. The author line shows the names the bundle carried, never a member of this portal who
happens to share an id. A range note whose lines changed since it was confirmed arrives `changed`:
mark it **Still true** or retire it as usual. A note the project retired is never brought back, and the
organization's own `[areas]` do not travel, so an area note needs that area on the target.

## Removing a project

Notes live in the project's WhyGraph database, so they follow its history; [export the project](../portal/transfer.md) first to keep them elsewhere. Removing a local project
keeps them unless you ask for the history to be deleted. Removing a production project deletes its notes
unless **Keep history** is ticked.

!!! info "Not available on a linked project"
    A [linked project](../portal/platform-projects.md) has no WhyGraph database on your machine, so it
    has no Notes page, its rationale cards carry no `notes`, and `whygraph_propose_note` and
    `whygraph://note/{id}` are refused there.

On a production portal, notes reach people through Chat and the Explorer only: there is no MCP
endpoint, and the platform API carries no notes. Confirming, marking still true and retiring a note are
written to the [security event log](../deploy/production.md#the-security-event-log) (the note id and
who acted, never its text). See [Security](../portal/security.md#project-notes) for how notes are
treated as untrusted input.
