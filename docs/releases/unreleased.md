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
