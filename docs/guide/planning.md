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
| `--refresh-profile` | Derive the [UI profile](#ui-evidence) again instead of reusing the cached one. |

`/whygraph-implement <plan.md | tasks.json> [flags]`

| Flag | Effect |
|---|---|
| `--from-step N` | Start at step N, and run step N again even when the log marks it done. |
| `--step N` | Run only step N. |
| `--commit-each-step` | Commit after each verified step. |
| `--fix-attempts N` | How many fix attempts a failing `Verify` gets. |
| `--model sonnet\|opus\|haiku` | Use this model for every step instead of each step's own. |
| `--no-review` | Skip the review gate. |
| `--no-eval` | Skip the [evaluator](#evaluation). |
| `--no-capture` | Skip [UI evidence](#ui-evidence) capture. |
| `--eval-fix-attempts N` | How many times a failed spec evaluation sends the step back to the implementor, 0 to 2. Default 1. |

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
| Evaluation | One short evaluator run per step on a fast model, then one run each for quality and UI on a stronger model. `--no-eval` skips it. |
| UI evidence | No model run for the capture itself. The UI stage reads the screenshots, and only that stage costs tokens. |

Rationale cards are read from the cache. A card that is missing may be generated on read, one LLM call
charged to the project's [budget](../portal/usage.md).

## Evaluation

Claude Code and Cursor add a separate evaluator, the `whygraph-evaluator` subagent. It is read-only,
runs after the implementor and judges the work against the plan, never against its own opinion of the
code. It runs in three stages:

1. **Spec**, after each step (and after its review gate in Claude Code). It checks that the diff
   touches only the step's **Files**, does what **Change** says and nothing more, leaves the
   **Boundaries** alone, and that the step's `Verify` command really exercises the changed symbol.
   The verdict is `pass`, `fix` or `escalate`. On `fix` the implementor is sent back with the findings,
   at most `--eval-fix-attempts` times (default 1), and then the loop halts. On `escalate` the plan
   itself is wrong, and the loop halts at once.
2. **Quality**, once, after the acceptance run. It reads every step's diff against your conventions
   file (when the plan names one) and a fixed list of six checks: surgical changes, no abstraction
   without a second case, names that match their neighbours, errors handled the way the file does,
   comments in the file's style, and no secret in code or tests.
3. **UI**, once, only when a step has **Expected UI** and [UI evidence](#ui-evidence) was captured. It
   checks each expected state against the screenshots and the page snapshots, then the screen's
   purpose, hierarchy, empty, loading and error states, the way back, and that untouched routes did not
   change.

Each stage prints a short report ending with a `## Confidence` line, and prints it in full only when it
has findings. `--no-eval` skips all three and runs the loop as before. VS Code and Codex have no
evaluator, because nested subagents are not available there; run `/whygraph-implement` from Claude Code
or Cursor to get it.

!!! warning "The model provider sees the evidence"
    The evaluator runs in your editor, against your own model account. What it reads goes to that
    provider: the diffs, and for the UI stage the screenshots and page snapshots. Do not capture a
    screen that shows data you may not send there. Use a fixture or a demo account, and `mask` in the
    profile for values that change on every load.

## UI evidence

A plan that changes a screen can capture the affected routes before and after each step, so you
compare pictures instead of trusting a green test run. WhyGraph installs no browser tooling and
fetches nothing: a small script, `capture.mjs`, drives the Playwright **you** installed in your
project.

### The UI profile

When a task touches a UI, the planner derives a profile of your front end and caches it in
`.whygraph/profiles/ui.md`: the stack, the command that starts the app and the directory to run it
in, the base URL, the routes and the theme mechanism, a login requirement, your browser tests, and
patterns for console and request noise. A second plan in the same project reuses it. The profile is
derived again when one of the files it came from changes, and `--refresh-profile` forces it.

The profile is read from an untracked file on purpose. It carries the start command the script runs,
so the planner and the script both refuse it when git tracks it. Keep it out of version control, and
do not copy one from a repository you do not trust.

| Key | Meaning |
|---|---|
| `start` and `start_dir` | The command that starts the app, and the directory to run it in. The script starts the server itself, reuses one that already answers, and stops the one it started. |
| `fixture` | A demo or fixture command to use instead of `start`, or `none`. |
| `allow_hosts` | Extra hosts the capture may visit. Loopback hosts are always allowed. |
| `mask` | Patterns for text that changes on every load, such as a clock. Matches are replaced before snapshots are compared. |
| `ignore_console` and `ignore_requests` | Known noise, such as a dev server's reload socket, that is not a defect. |
| `capture` | `available`, `unknown` or `unavailable: <reason>`, from a probe the planner runs. |

### Your UI standards

Write your team's UI standards under a `## UI conventions` heading in an always-on file the planner
already reads: `CLAUDE.md`, `.claude/CLAUDE.md`, `AGENTS.md` or `.github/copilot-instructions.md`.
The planner records where the heading is and never copies the text into a plan. In Cursor, put the
section in your own rule file, `.cursor/rules/ui-conventions.mdc`. Do not edit WhyGraph's bundled
rule files: they are tracked by hash, and an edit would show as **Edited** on every update.

### Installing Playwright

Capture needs Node 18 or newer and the Playwright library at a recent version in your repository or in
a front-end directory, with its Chromium browser installed. That is yours to install. WhyGraph never
runs `npm install` or `playwright install`. When something is missing the script stops, names what it
looked for and prints the exact command to run, for example in the front-end directory:

```bash
npm i -D playwright
npx playwright install chromium
```

### What it captures

For each route, the script takes a full-page screenshot and a page snapshot (the accessibility tree
as text) in a desktop and a phone viewport, in a light and a dark theme when the profile names a
theme mechanism, with animations switched off. It records console errors, failed requests, horizontal
overflow, and controls with no accessible name. Routes must be plain paths on your own app. A route
that tries another host, and a server redirect that leaves the allowed hosts, ends the capture with
no evidence for it.

```text
.whygraph/plans/<plan>/ui/
  index.html                 one contact sheet over every step
  <N>/before/  <N>/after/    screenshots and page snapshots
  <N>/signals.json           console errors, failed requests, overflow, unnamed controls
  <N>/diff.json              which routes changed
  <N>/sheet.html             the step's before and after sheet
```

Open `index.html` to compare. The sheets are static pages with no script, and all text from your
pages is escaped. The before capture runs once and is kept. After a step, a new console error,
failed request, phone-width overflow or unnamed control that shows up on two runs in a row halts the
loop, with the signal quoted. You can also run it by hand in Claude Code and Cursor:
`/whygraph-ui-check <route...> [--tag before|after] [--out DIR]` prints the path of the sheet.
`--no-capture` skips capture for a run.

### Without Node

When the planner's probe cannot find Node, the profile, Playwright or the browser, it writes
`capture: unavailable` with the reason, and nothing fails. The plan gets one `manual:` Acceptance
item per expected UI state instead of the contact-sheet item, the implement command passes no capture
command, and the UI evaluation stage does not run. Spec and quality evaluation still run. Plain
screenshots are the only thing lost: the plan, the implementation and the tests are unchanged.

### Limits

A full-page screenshot misses content in an inner scroll container, so the sheet says so and the page
snapshot still covers that content. Codex and VS Code run the capture script when the profile says it
is available, but they have no evaluator to judge the result, and the VS Code planner has no terminal,
so it writes the profile with `capture: unknown` and the script checks for itself. An edited
`capture.mjs` in a cloned repository is code that an agent runs, like any committed script: read it
after a clone, as you would the `sources/` folder.

## Differences between editors

| | Claude Code | Cursor | VS Code | Codex |
|---|---|---|---|---|
| Research fan-out | Yes | Yes | Yes | Yes |
| Researcher tool limits enforced by the editor | Yes | Yes | Yes | **No** |
| Medium source | Yes | Yes | No | Yes, unenforced |
| One implementor per step, at the step's model | Yes | Yes, but every step on one model unless you pass `--model <model-id>` | No, one run | No, one run |
| Review gate after each step | Yes | Skipped | Skipped | Skipped |
| Evaluator (spec, quality, UI) | Yes | Yes, without a review gate | No | No |
| UI evidence capture | Yes | Yes | Yes | Yes |
| `/whygraph-ui-check` by hand | Yes | Yes | No | No |
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
