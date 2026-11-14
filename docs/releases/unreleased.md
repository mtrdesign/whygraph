# Unreleased

!!! note "Not released yet"
    These changes are on `main` and ship in the next release.

## Changes

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
