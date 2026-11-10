# Unreleased

!!! note "Not released yet"
    This page collects what is going into the next release. It moves into a numbered release page when
    that release is cut.

## What's new

- **Best practices from the web for `/whygraph-plan`.** A new bundled research source,
  `best-practices.md`, searches the web for current guidance on the task's design choice and cites
  each finding by title, URL and year. It always asks first, sends only the search terms, and never
  opens a page. Its findings rank below your repository's own evidence: they appear only under the
  plan's `## Prior art`, tagged `(external)`, and never change a step, a constraint or the plan's
  confidence. Claude Code only. See
  [Best practices from the web](../guide/planning.md#best-practices-from-the-web).

## Fixes

- **The Describe now estimate matches what a run spends.** It undershot the real cost by about 3x
  ([#86](https://github.com/mtrdesign/whygraph/issues/86)). It now replays how a run splits each
  commit: it sizes every file of a commit, packs them into chunks the way the describer does, counts
  one model call per chunk plus the call that merges them, and counts merge commits by their changes
  against the first parent (it left them out, though they are described). Its sizes are measured on
  two repositories, and it knows how `claude-haiku-4-5` and `claude-opus-4-7` differ from
  `claude-sonnet-5-5`: each lands within 5 % of what a sample really cost. Another model is estimated
  as `claude-sonnet-5-5`.
- **Large merge commits get the cheap stub.** A merge touching more files than
  `[analyze].large_commit_file_count` against its first parent is no longer described in one pass;
  like any large commit, its files are described on demand.
