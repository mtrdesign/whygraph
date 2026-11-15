# Unreleased

!!! note "Not released yet"
    These changes are on `main` and ship in the next release.

## Changes

- **A Test first test is seen red, and cannot be edited to pass.** `plan-tool.sh red` runs the
  test before the change and writes `steps/<N>.red`. `verify` and `end` compare the test with a copy
  and stop on an edit, with the first line `verify: test first edited`. `proof` takes the class of
  a Test first step from that record (`proven`, `not proven` or `edited`), and each step's diff adds
  `tests:` lines for a skip marker, removed assertions, a changed snapshot or runner config. The
  dry run no longer reports an existing Test first test that fails by design. Run **Settings >
  Update agent files** to pick it up, and see
  [Verify evidence](../guide/planning.md#verify-evidence).
- **The run summary says how many steps are proven.** `plan-tool.sh proof <plan>` prints one line for
  each step (proven, unproven, carried, not proven, no test, failed, not run or unknown) and two total
  lines. The run summary of `/whygraph-implement` in Claude Code and Cursor shows the totals as
  `- Proof:`, and the implementor logs them under `- Acceptance:` in VS Code and Codex. An `unproven`
  step asks a person to read its diff first. Run **Settings > Update agent files** to pick it up, and
  see [Verify evidence](../guide/planning.md#verify-evidence).
- **A plan checks the user's path through the screens.** When a task moves the user between
  screens, the plan gets a `## User journey` section (the actor, the entry control, one line per hop
  tied to a step or to existing code, the outcome, what holds on refresh, back, cancel and a direct
  link, and what the user sees while slow work runs). In Claude Code a journey reviewer checks it
  against the router and the code, and `plan-tool.sh check` lints its lines. A plan's `## UI profile`
  now also carries `**Auth:**`, so the UX/UI reviewer asks for a no-permission state when a login is
  required. Run **Settings > Update agent files** to pick it up, and see
  [Plan review](../guide/planning.md#plan-review).
- **Every constraint on a rationale card names its source.** Each item of a card's `constraints`
  and `risks` is now an object with its `text`, a `basis` (`quoted`, `cited`, `inferred` or
  `unknown`) and the commits, PRs or issues behind it. The planning agents quote the `text` as before
  and end each quote with a tag, such as `(quoted: 1a2b3c4d)` or `(inferred)`. A constraint the model
  only inferred never becomes a Blocker, and in Claude Code a review-gate verdict on one is printed
  and does not hold the step. A card cached before this release reads `unknown` until it is generated
  again. Run **Settings > Update agent files** to pick it up, and see
  [Where a constraint comes from](../guide/planning.md#where-a-constraint-comes-from).
- **A plan can change a recorded constraint on purpose.** When the task itself asks to change a
  constraint, the step gets a **Changes on purpose** line with the quote and the reason, in place of
  a Blocker. The implementor does not guard it, and in Claude Code the review gate reports it as
  **Changed on purpose** and goes on. Run **Settings > Update agent files** to pick it up.
- **A plan runs before you read it, so it can be trusted to run.** The planner now runs each `Verify`
  once and hands over the result (the dry run), and a plan can carry a `## Setup` section of install and
  generate commands. A run that finds a `Verify` failing before any change runs the Setup, tries again,
  and stops with `Verify fails before any change` or `setup failed` instead of failing late. A `manual:`
  span in backticks is no longer run as a command. The dry run executes commands from your repository
  before you read the plan, with a smaller set of secrets and no package download. Pass `--no-dry-run`
  to skip it. Run **Settings > Update agent files** to pick it up, and see
  [The dry run](../guide/planning.md#the-dry-run) and
  [Running unattended](../guide/planning.md#running-unattended).
- **A run proposes the note it made stale.** After a `/whygraph-implement` run whose Acceptance
  passed, Claude Code and Cursor propose a note for each step with a **Changes on purpose** line, at
  most three per run: a revision of the note the step changed (its `(note #<id>)` tag stays on the
  line), or a `decision` note that says the quoted constraint no longer holds. Nothing counts until a
  person confirms it, and confirming a revision retires the old note. VS Code and Codex print one line
  per step instead. See [Limits on agent proposals](../guide/notes.md#limits-on-agent-proposals).
- **A Verify that ran no test is marked, and a run starts with a brief.** A `Verify` that exits 0
  while its output says no test ran (such as `collected 0 items` or `0 passing`) is recorded at
  baseline as `pass (no test ran)`: still a pass, but now visible. In Claude Code and Cursor,
  `/whygraph-implement` first prints a one-page brief of the plan, built with no model by the new
  `plan-tool.sh brief <plan>`. Run **Settings > Update agent files** to pick both up, and see
  [Running unattended](../guide/planning.md#running-unattended).
- **A constraint note can carry a check.** Add a check to a `constraint` note: a text that must not
  appear, or must appear, in the files a glob names. The Notes page shows it and, on a single note,
  whether it holds. `/whygraph-plan` copies it into the plan and `/whygraph-implement` searches for it
  after every step; a broken check sends the run to the constraint rung (Claude Code) or fails the
  step's Verify (the other editors), unless the step's **Changes on purpose** names the note. A check
  is a text search and never runs a command. See
  [A check on a constraint](../guide/notes.md#a-check-on-a-constraint).
- **The platform API is version 2.** A rationale card's `constraints` and `risks` are lists of
  items with their sources, so a linked portal and its platform must both run this release. An older
  linked portal shows **Update required** until it is updated.
