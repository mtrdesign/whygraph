# Planning a change

`/whygraph-plan <task>` turns a task into a step-by-step plan that rests on your code graph and your
project's recorded rationale. `/whygraph-implement <plan>` then carries the plan out, one step at a
time. Both commands are installed with your [agent files](../portal/agents.md) and run in your editor.
Neither one is a new MCP tool: they are skills and subagents that call the tools you already have.

!!! note "Plans are local"
    A plan is written to `.whygraph/plans/`, which is gitignored. Use `--out <path>` to put the plan
    file somewhere else, for example to commit it. Research notes and split files stay under
    `.whygraph/plans/` whatever `--out` says.

## The pipeline

```text
task -> triage -> research (one researcher per source) -> synthesis -> questions
     -> implement (one step at a time, review gate after each) -> acceptance
```

1. **Triage.** The planner checks the size of the change with CodeGraph. It splits the task into
   child plans only when all three signals agree: the impact set is large (more than 15 symbols in at
   least 3 directories), the task text names at least 2 separable deliverables, and you did not pass
   `--no-split`. Otherwise you get one plan. `--split` forces a split. At most 6 children are made, each a
   vertical slice, ordered into waves by what depends on what.
2. **Research.** The planner reads the [sources](#sources) folder and starts one researcher for each
   source it can use. Small, obvious tasks skip this stage and run in a single pass.
3. **Synthesis.** One synthesizer merges the reports into one plan: Objective, Non-goals, Working
   set, Prior art, Blockers, Steps, Risks, Acceptance, Open questions and Confidence. Every step names
   its files, its boundaries, a literal `Verify` command and the model that should run it.
4. **Questions.** You are asked after research, never before it. At most three questions, each with
   options and a default, and only when your answer changes the shape of the plan. The planner then
   revises the plan with your answers. `--no-questions` takes every default.
5. **Implement.** `/whygraph-implement` starts a fresh implementor for each step, at that step's model.
   Before it edits, it runs a baseline of the step's `Verify` commands. A step that begins with a
   `Test first` line adds that test, sees it fail, and only then changes code.
6. **Review gate.** After each verified step, a reviewer checks the step's own diff against the
   rationale cards already cached. A **Constraint at risk** verdict stops the loop so that you can fix
   it. The gate reads cached cards only and never generates one.
7. **Acceptance.** A last, separate run walks the plan's `## Acceptance` list: first the project's full
   test command, then each check. It reports what failed and undoes nothing.

The implementor flags files it changed outside the step's **Files**, logs every fix attempt with its
cause, and never edits a file that the step's **Boundaries** names.

## Commands and flags

`/whygraph-plan <task> [flags]`

| Flag | Effect |
|---|---|
| `--shallow` / `--deep` | Force a single pass, or force the research fan-out. Not both. |
| `--split` / `--no-split` | Force a split into child plans, or forbid one. |
| `--sources <a,b>` | Run only these sources. A source with `enabled: false` is turned on for this run. |
| `--no-questions` | Take every default. Every consent question is answered No. |
| `--out <path>` | Save the plan at `<path>` instead of `.whygraph/plans/<name>.md`. |

`/whygraph-implement <plan.md | tasks.json> [flags]`

| Flag | Effect |
|---|---|
| `--from-step N` | Start at step N, and run step N again even when the log marks it done. |
| `--step N` | Run only step N. |
| `--commit-each-step` | Commit after each verified step. |
| `--fix-attempts N` | How many fix attempts a failing `Verify` gets. |
| `--model sonnet\|opus\|haiku` | Use this model for every step instead of each step's own. |
| `--no-review` | Skip the review gate. |

The implementor refuses a plan that has open Blockers or an unanswered plan-shaping question.

## Sources

Research does not come from a fixed list. It comes from the files in a `sources/` folder beside the
skill. One file is one source of knowledge. The planner reads the frontmatter of each file, keeps the
sources whose tools exist in your session, and starts one researcher per kept source.

| Editor | Where the sources live |
|---|---|
| Claude Code | `.claude/skills/whygraph-plan/sources/` |
| Cursor | `.cursor/skills/whygraph-plan/sources/` |
| VS Code | `.github/whygraph/sources/` |
| Codex | `.codex/whygraph/sources/` |

Five sources ship with WhyGraph:

| File | What it reports | Reaches |
|---|---|---|
| `code-structure.md` | Boundaries and external contracts for each step | CodeGraph |
| `whygraph-rationale.md` | Constraints and risks, quoted verbatim from rationale cards | WhyGraph |
| `repo-tests.md` | The commands that prove each step, and symbols no test covers | CodeGraph |
| `repo-history.md` | Prior art in your own commit and pull request history | WhyGraph |
| `medium.md` | Practitioner articles on the design choice | The Medium MCP server |

Your own files survive an update of the agent files. Delete a file to remove its source.

### Adding a source

Write one file in the folder. This one reads architecture decision records that your repository keeps
under `docs/adr/`:

```markdown
---
name: adr
description: Architecture decision records in this repository that bear on the task.
requires:
tools: read
section: Prior art (ADRs)
enabled: true
budget: 15
consent: false
---

*Question:* which architecture decision records in this repository bear on this task?

1. Find the folder with `Glob`, trying `docs/adr/*.md`, `docs/decisions/*.md` and `adr/*.md`.
2. Read the title and status of each record. Skip records that are superseded or rejected.
3. Read in full only the records that name a file, symbol or area from the working set.
4. Report each match under the section as: the record's title, its path, and the decision in one sentence.
5. Quote any constraint in the record verbatim. Never paraphrase a constraint.
6. Report `none found` when the repository has no such folder. Do not search elsewhere.
```

Put exactly these eight keys in the frontmatter, with no comments. The parser reads each whole value.

| Key | Meaning |
|---|---|
| `name` | The name of the report. Use kebab-case. |
| `description` | One line. The consent question shows it for a file you added. |
| `requires` | Bare tool names, separated by commas. The planner keeps the source only when every tool is callable. Empty means always eligible. |
| `tools` | The tools this source may use: `read` plus bare MCP tool names. It narrows the researcher's list and never widens it. A shell, a file write and a web tool are never valid. |
| `section` | The heading of the report section this source fills. Each source needs a unique one. |
| `enabled` | `false` keeps the file but never starts it. `--sources` turns it on for one run. |
| `budget` | The most tool calls the researcher may make for this source. |
| `consent` | `true` forces the consent question. It cannot switch off a question the entry point computes. |

Write a tool by its bare name, for example `codegraph_search` or `whygraph_area_history`. A bare name
matches a session tool whose name equals it, or ends in `__<name>` or `/<name>`. The word `read` means
reading and searching files.

!!! warning "A source that needs another MCP server needs one more edit"
    A researcher can call only the tools in its own `tools` line. That line lists the `whygraph`,
    `codegraph` and `medium` servers. If your source needs a different MCP server, add that server to
    the `tools:` line of the researcher agent file by hand. The next **Update agent files** lists that
    file as edited and leaves it alone, as it does for any file you changed.

### Consent

The plan command asks once per run, in one dialog and with **No** as the default, before it starts a
kept source that does any of these:

- names an MCP tool other than a `whygraph_*` or `codegraph_*` tool;
- is not one of the bundled files in the table above;
- says `consent: true`.

A file you add always triggers the question, and the question says it was added to your repository
and not shipped with WhyGraph. A file cannot switch the question off, because a clone of someone
else's repository may carry sources of its own. After you clone a repository, read its `sources/`
folder before you run a plan.

For a source that reaches an outside service, the question also shows the **search terms**: one to
three technical topics taken from your task. Only those terms leave your machine. File content, symbol
names, commit messages and project names are never sent. In a split, only the first child asks.

### Medium

`medium.md` is the one bundled source that reaches an outside service. It searches Medium for
practitioner articles on the design choice and cites them by title and URL. It paraphrases and never
copies a passage, and it treats article text as someone's opinion, never as an instruction.

- It runs only when the Medium MCP server is configured and your session can call `search_medium`.
  Without the server the planner drops the source and nothing breaks.
- It always asks first and shows the search terms. The Medium server limits how many searches and
  articles one plan may use, and each call takes a few seconds on your screen.
- **Name the server `medium`.** The researcher's tool grant is `mcp__medium__*`. Under another name
  `requires: search_medium` still matches, but the researcher reports the tool as not granted. If you
  named the server otherwise, edit that grant in the researcher agent file.
- It works in Claude Code and Cursor. Codex can run it too, but only because Codex cannot block it
  (see below). The VS Code researcher has no Medium grant, so the source does not run there.

## Splitting: `tasks.json`

When triage splits a task, the planner writes `.whygraph/plans/<name>.tasks.json` once and returns.
The command then starts a fresh planner for each child, which writes `.whygraph/plans/<name>-t<N>.md`.
Children never write the manifest.

Pass the manifest to `/whygraph-implement` instead of a plan. It runs the children in wave order, one
at a time, and skips a child already marked `done`. It stops when a child's dependencies are not all
`done`. The acceptance run of each child sets that child's `done` or `acceptance_failed` status.

The manifest is checked before use, and a failed check refuses it with the reason: it must not be
tracked by git, `version` must be 1, there are at most 6 children with ids `t1` to `t6`, each `plan`
path must be one of the `.whygraph/plans/` child files, every dependency must exist, and there is no
cycle.

## What each stage costs

Every stage runs in your editor, against your own model account.

| Stage | Cost |
|---|---|
| Triage | A few CodeGraph queries. No extra agent. |
| Research | One researcher per kept source, on a fast model, with the source's `budget` as a cap on tool calls. This is the largest part of a plan. `--shallow` skips it. |
| Synthesis | One synthesizer run. |
| Questions | No model run. One dialog, then one short revise pass. A revise where you took every default does not run the research again. |
| Implement | One implementor per step, each at the model the plan names for it. A fresh context per step keeps the runs small, and a cheap step can run on a cheap model. |
| Review gate | One reviewer run per step. It reads cached cards only, so it adds no WhyGraph LLM spend. `--no-review` skips it. |
| Acceptance | One more implementor run. |

Rationale cards are read from the cache. A card that is missing may be generated on read, one LLM call
charged to the project's [budget](../portal/usage.md).

## Differences between editors

| | Claude Code | Cursor | VS Code | Codex |
|---|---|---|---|---|
| Research fan-out | Yes | Yes | Yes | Yes |
| Researcher tool limits enforced by the editor | Yes | Yes | Yes | **No** |
| Medium source | Yes | Yes | No | Yes, unenforced |
| One implementor per step, at the step's model | Yes | Yes, but every step on one model unless you pass `--model <model-id>` | No, one run | No, one run |
| Review gate after each step | Yes | Skipped | Skipped | Skipped |
| Acceptance | Separate run | Separate run | At the end of the run | At the end of the run |
| Questions | Dialog | Asked in chat | Asked in chat | Asked in chat |
| Splitting | Child plans started for you | Child plans started for you | Run the prompt once per child | The main agent runs one implementor per child |

In VS Code the prompt runs as the planner itself, so it asks its questions in the same conversation
and has no terminal. In Cursor the plan is run from a skill, and the review gate is skipped; run
`/whygraph-review` from Claude Code if you want it.

!!! warning "What Codex cannot enforce"
    Codex documents no per-agent tool allowlist. In the other editors the researcher is held to a list
    of read-only tools by the editor itself. In Codex the researcher's written rules are the only
    control. A hostile source file could ask a Codex researcher to run a shell command, and a Codex
    researcher can reach Medium. The consent question still protects you from a repo-added source, but
    it cannot stop a bundled file that someone edited. Review `sources/` after a clone, and review
    changes to it in a pull request.

Next: [wiring your editor](editors.md), how an agent [calls the tools](mcp-usage.md), and the
[project notes](notes.md) the planner reads.
