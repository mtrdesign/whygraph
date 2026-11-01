# Export and import a project

A **bundle** is one `.zip` file that carries what WhyGraph learned about a project - its
[notes](../guide/notes.md), its LLM output and, if you ask, your Chat conversations - so you can
move it to another portal, or to another project of the same repository, without paying for the
model calls again. The Git and GitHub data are not in it: the target scans its own copy of the
repository.

Use it to start a production project with what you built locally, to bring a production project's
notes back to your machine, or to seed a second portal. To move a **whole** portal, use
[Backup and restore](backup.md) instead.

## What travels and what does not

| Travels | Stays behind |
|---|---|
| Project notes: confirmed, proposed and retired, with their anchors, revision chains and the names of who proposed and confirmed them | Rejected notes and pending agent proposals |
| The purpose card and the runs that drafted it | Commits, file changes, pull requests, issues, tags and the CodeGraph index (the target scans them) |
| Commit and file **descriptions** | Keys, tokens, passwords and every secret |
| Rationale cards | Other people's chats, and ratings |
| Your chats, when you tick **Include my chats** | Scan settings (`[scan]`), hooks, the organization's `[areas]` and every [organization-only limit](projects.md#settings) |
| A few settings: the models (`llm.model`, `llm.<provider>.model`) and the `[analyze]`, `[rationale]`, `[chat]` and project-level `areas` sections - only when you opt in | Usage and cost records |

All four directions work: local to local, local to production, production to production and
production to local. A bundle names the repository by its root commits, so it can only be imported
into a project of **the same repository**; any other repository is refused.

!!! note "Not available on a linked project"
    A [linked project](platform-projects.md) has no WhyGraph database on your machine. Export and
    import answer `403 managed_on_platform` there; use the platform project instead.

## Who can do it

Export, import and removing imported rationale cards need the `project.configure` permission: a
project admin in production, and the single local user on a local portal. Contributors and viewers
get `403 forbidden`. The project must have finished its **first scan**, on the source for an export
and on the target for an import (`409 transfer_needs_scan` otherwise).

## Export

Open **Settings** for the project and find **Export and import**.

1. Tick **Include my chats** if you want your conversations to go with it. On a production portal
   only the chats **you** own travel; on a local portal, also the chats from before accounts existed.
   Ratings never travel.
2. Choose **Export**. The browser downloads `whygraph-<slug>-<date>.zip`.

Before you remove a project, the **Remove project** dialog links to this section ("Export this
project first").

!!! warning "The bundle is confidential"
    It holds your notes, and with chats on it can hold tool output with **file contents** the
    assistant read. Treat it like the repository. Keep it somewhere private and delete it after the
    import.

## Import

There are two places, and they open the same preview.

- **The add-project wizard.** On the Configure step, once the first scan is `ok`, a card "Moving
  from another portal? Import a bundle first" sits above the describe and purpose options. Import
  first: the commits the bundle already describes are then not described again, and an imported
  purpose card shows in the purpose option.
- **Settings, Export and import.** Choose a bundle file. The import is disabled with "Run the first
  scan first" until the project has been scanned.

Choosing a file never changes anything: it runs a **dry run** and opens the preview.

### The preview

The dry run does everything an import does and rolls it back, so the counts are what **Apply** will
do (as long as nothing changed in between). It shows:

- **The source**: its label, mode (local or production), WhyGraph version and date, and the line
  "Stated by the bundle: same repository" with both origins. The bundle states these itself; the
  repository check is on the root commits, not on that text.
- **Counts**: notes added by status, notes skipped as duplicates, skipped for size, denied or
  unresolved; rationale cards added or already there; descriptions filled and kept; purpose runs;
  chats and messages.
- **Confirmed notes that will reach every agent session.** A project-wide note, or a note on a
  top-level folder or a broad area, is read by nearly every Chat turn and Claude Code session
  (see [Notes that reach every session](../guide/notes.md#notes-that-reach-every-session)). Imported
  confirmed notes **stay confirmed**, so the preview lists every one of these with its **full text**.
  Read them before you apply. Other confirmed notes appear as counts only.
- **Chats**: "N chats will be added to **your** chats", and which providers they use.
- **Settings**, when the bundle has any: the exact keys that would change, with the old and new value.
  See [Settings](#settings).
- A note that Chat and agents on the project may pause for a moment while it applies.

**Apply** sends the same file again. The result view shows what was added.

Every record of every member is checked before anything is written, so a malformed bundle changes
nothing (and writes no backup). If writing the rationale cards or descriptions fails part of the way
through - the database stayed busy for more than five seconds, or the disk is full - what was already
written stays, the rest is not written, and the result says **Partially imported** (the API marks the
`rationale` / `descriptions` block `"partial": true` with an `error`). The notes, chats and settings
are not affected. Importing the same bundle again adds the rest, since the target's data always wins.

### Rules the import follows

- **The target's data always wins.** A description is filled only where the target has none. A rationale
  card is added only for a place and model that has no card. A note the target already has (same
  text, anchor and kind) is skipped, and a note the target **retired** never comes back.
- **Re-importing is safe.** The same bundle twice adds nothing the second time, and a round trip back
  to the source adds nothing either.
- **Imported notes keep their status.** Confirmed stays confirmed, with the names of who proposed and
  confirmed it from the bundle. They carry an **imported** badge in the Notes page and wherever a note's
  author is shown, and they never become the name of a real member of the target even when the ids
  match. A note whose anchor is a range of lines is `current` if those lines are unchanged in the
  target and `changed` otherwise - **Still true** it, as usual. If a project already has a live purpose
  card, every purpose note and run in the bundle is skipped.
- **Never two confirmed versions of a note.** When the source confirmed a revision of a note that the
  target still has confirmed (say, after an earlier import), the revision arrives as a **proposed**
  revision of the target's note and is counted as `downgraded`. Confirming it retires the target's
  version as usual; until then agents keep reading the target's.
- **Chats become yours.** They are added to the importer's chats (on a local portal, to the single
  local user's) and can be continued or deleted like any other. A chat the importer already has is not
  added twice.
- **Cards are reused.** A rationale call for an imported place with the same provider and model makes
  no model call.

### Settings

The settings in a bundle are applied only if you tick **Apply these settings** in the preview. It is
off by default. When ticked, **the bundle wins** for every key it carries: its values replace the
target's project-level values for those keys, and nothing else changes. The preview shows the diff
first and disables the box when the settings would not be accepted. Only the keys in the table above
can travel, and on a production portal only those the production allowlist lets you write. If the
settings cannot be applied after the data is committed, the result says `config.applied: false` and
gives the reason; the data stays imported.

The organization's own `[areas]` (an org-level layer) never travels. A note anchored to an area needs
that area to exist on the target; project-level `areas` in the bundle can bring it with them.

## Remove imported rationale cards

Imported cards are marked with the import that brought them. **Settings, Export and import** shows
**Remove imported rationale cards (N)** when there are any, and removes **exactly** those, nothing the
target generated itself. It exists because a rationale card is model output that agents trust: if you
do not trust a bundle, remove its cards and they are generated again on demand. Descriptions are not
marked and stay; they are only length-checked. The list under the button shows the latest imports.

## Backups and undoing an import

Before an apply, the portal copies the project's WhyGraph database to
`<data>/projects/<org>/<slug>/backups/whygraph-import.<UTC time>.db` (file mode 0600) and keeps the
newest **3**. To undo an import, stop the portal, put one of those files back as
`whygraph.db`, and start it. A backup also still has any chat you deleted after it was made. It is
skipped, and the preview says so, when the disk has less than twice the database's size free.

On a **local** portal the result shows the path. A **production** portal writes the same backup on the
server and does not reveal the path; a restore there is an operator's job
([Backup and restore](backup.md)).

## Limits

| Limit | Value |
|---|---|
| The `.zip` file | 100 MiB |
| All members together, uncompressed | 400 MiB |
| One line of a member | 8 MiB |
| People, notes, purpose runs and chats together, uncompressed | 48 MiB |
| Rows: notes | 5,000 |
| Rows: rationale cards | 50,000 |
| Rows: commit descriptions | 200,000 |
| Rows: file descriptions | 500,000 |
| Rows: chat sessions / chat messages | 500 / 12,500 |
| Rows: people / purpose runs | 10,000 / 100 |
| Exports per member and organization | 10 per hour |
| Imports and dry runs, per member and organization / per project | 20 per hour each |

The row limits, and the 48 MiB limit on people, notes, purpose runs and chats, keep one import from
holding the project's database locked for long. Those are written in **one transaction** - all or
nothing - which measures about half a second at the row limits with short chats, and about a second
and a quarter at 48 MiB of long ones. Rationale cards and descriptions follow in batches of 10,000, each its own
short transaction (about a tenth of a second each), so a scan or a Chat turn never waits long for an
import. A project with more rows than a limit, or with more than 48 MiB of people, notes, purpose runs and
chats, **cannot be exported**: the export stops with `413 bundle_too_large` (drop **Include my
chats** if chats are what is over). Only one export or import runs at a time per
organization, and two per portal; another at the same moment answers `409 busy`, as does a project
that is scanning, being removed or importing already. Exports and imports are streamed through
temporary files in `<data>/tmp`, removed after each request and swept at start-up.

A single field that is too long (a note body, a description, a chat message) is **skipped** and
counted as `skipped_oversize`; a whole chat with one such field is skipped, so that no message loses
its parent.

## Errors

| Code | Status | When |
|---|---|---|
| `bundle_invalid` | 400 / 415 | Not a zip, a damaged or inconsistent bundle, a malformed record (the member and line are named), or a content type other than `application/zip` (415) |
| `bundle_version` | 400 | A bundle from a format this WhyGraph does not read; the message names both WhyGraph versions |
| `bundle_too_large` | 413 | The export or the bundle is over a size or row limit |
| `bundle_wrong_repo` | 409 | No root commit of the bundle is a commit of this project's repository |
| `transfer_needs_scan` | 409 | The source or the target has not finished its first scan |
| `busy` | 409 | Another transfer, a scan or a removal is using the project, the organization or the portal |
| `throttled` | 429 | Past the hourly limits |
| `body_too_large` | 413 | The upload is over 100 MiB |
| `managed_on_platform` | 403 | A [linked project](platform-projects.md) |
| `forbidden` | 403 | Not a project admin |

## Security notes

A bundle is **untrusted input**, even one you exported yourself: it can come from anyone. The
importer opens it as an archive and never extracts it; entry names, sizes, counts and hashes are
checked before any line is parsed, and every record is validated before anything is written. A
hostile bundle is refused with nothing written. See [Security](security.md#project-transfer).

Production logs `project_bundle_exported`, `project_bundle_imported` and `imported_rationale_removed`
to the [security event log](../deploy/production.md#the-security-event-log): ids, counts, the bundle's
hash, never any text.
