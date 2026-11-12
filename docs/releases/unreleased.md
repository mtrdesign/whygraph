# Unreleased

!!! note "Not released yet"
    These changes are on `main` and ship in the next release.

## What's new

- **A cheaper Describe core.** Describe core gets a checkbox, **Also describe large commits file by
  file** (ticked by default, so nothing changes unless you untick it). Unticked, Describe core
  describes only the core commits and leaves the per-file changes of large commits in core files
  until a question reaches them. On a repository with many large feature merges those file changes
  are most of the cost: on one 4,100-file application they were $1.17 of Describe core's $1.74. The
  estimate card shows what they add, and the Describe core button's price follows the checkbox.
  Describe all and Full rescan still describe them.

## Changes

- **Plan agents can stop their background tasks.** The `whygraph-planner`, `whygraph-synthesizer`
  and `whygraph-implementor` agents get Claude Code's `TaskStop` tool, so a command one of them runs
  in the background can be stopped instead of left running. Run **Settings > Update agent files** to
  pick it up.
- **`/whygraph-implement` runs unattended, and `/whygraph-plan` reviews its plan.** An implement run
  now needs no flag: a script probes and baselines every `Verify` and Acceptance Check before the first
  edit, a counted recovery ladder (a retry on the next model up, one amendment of a step's `Verify` or
  `Files` line, a findings re-run) handles a failing step, every `Verify` has a deadline
  (`WHYGRAPH_VERIFY_TIMEOUT`, 540 s by default), and every stop prints one block with its cause and the
  next action. `--fix-attempts` now defaults to 2, and the command pre-approves `plan-tool.sh`, so a
  plan's `Verify` lines run without a prompt: run only a plan you reviewed. In Claude Code the ladder
  and pre-flight are complete; Cursor has them on one model without the review gate; VS Code and Codex
  get the deadline and the new default. Before it asks its questions, `/whygraph-plan` also sends a deep
  plan to a logic reviewer, and a UX/UI reviewer for a screen change, then a fixer applies only the
  findings it verifies (at most 25% growth). `--review` forces it, also for a single-pass plan, and
  `--no-review` skips it. Run **Settings > Update agent files** to pick it up, and see
  [Planning a change](../guide/planning.md).
