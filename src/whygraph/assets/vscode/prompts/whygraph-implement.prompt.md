---
description: Execute a reviewed WhyGraph plan (or one child of a split's tasks.json). Reads the plan as a strict contract, runs Verify after each step, halts on first failure, then walks the plan's Acceptance list.
name: whygraph-implement
argument-hint: <plan.md | tasks.json> [--from-step N] [--commit-each-step] [--fix-attempts N] [--model <model-id>] [--no-capture]
agent: whygraph-implementor
---

# /whygraph-implement - execute a reviewed WhyGraph plan

Execute a reviewed WhyGraph plan (or one child of a split's `tasks.json`). Reads the plan as a strict contract, runs Verify after each step, halts on the first failure, walks the plan's `## Acceptance` list at the end, and appends an Implementation log to the plan.

This prompt runs **as** the implementor, so every step runs in this one conversation. The per-step review gate needs Claude Code: run `/whygraph-review` there when you want the step's diff checked against the recorded rationale. The evaluator stage is unavailable here (nested subagents are not available); run `/whygraph-implement` from Claude Code or Cursor for it.

**Argument shape:** `<plan.md | tasks.json> [--from-step N] [--commit-each-step] [--fix-attempts N] [--model <model-id>] [--no-capture]`

## Step 1 - Parse arguments

Strip and remember these flags if present (any order, anywhere in the user's input):

- `--from-step <N>` -> integer >= 1 (consume the next token as `N`). Run step N through the last step. Default: the implementor picks the first step not marked `[x]` in the plan's `## Implementation log`, or step 1 if no log exists.
- `--commit-each-step` -> boolean flag (default `false`).
- `--fix-attempts <N>` -> integer 0-2 (consume the next token; default `0`, values above 2 become 2). On a failed Verify the implementor makes up to `N` bounded fixes of the same step's files, re-running Verify each time, before it halts.
- `--model <model-id>` -> optional override (consume the next token). Default: the implementor subagent's inherited model.
- `--no-capture` -> pass `CAPTURE: none` to the implementor (Step 2b).

The first non-flag positional token is the **path**: a plan (`.md`) or a split's `.tasks.json`. If it is empty, respond with:
`Usage: /whygraph-implement <plan.md | tasks.json> [--from-step N] [--commit-each-step] [--fix-attempts N] [--model <model-id>] [--no-capture]` and stop.

If the path ends in `.tasks.json` and `--from-step` is passed, respond with `--from-step takes a plan; pass the child's plan file instead of the tasks.json.` and stop.

## Step 2 - Pre-flight validation (read-only)

Resolve the path against the current working directory and make it absolute. Read it. If the file does not exist, respond with the error verbatim and stop.

**A tasks.json.** Implement one child per run. Check each rule below. On a failed check, respond with `Refusing <path>: <reason>.` and stop.

1. The path ends in `/.whygraph/plans/<stem>.tasks.json`. The repo root is the directory that holds `.whygraph/`.
2. The file parses as JSON and has `version: 1`.
3. `children` has 1 to 6 entries. Each `id` is one of `t1`-`t6`, and no `id` repeats.
4. Every `plan` matches `^\.whygraph/plans/[a-z0-9-]+-t[1-6]\.md$`.
5. Every `depends_on` entry names an existing `id`, and the dependencies have no cycle.

Then pick the child: the first one, in ascending `wave` and then `id` order, whose `status` is not `done`.

- No such child -> print `Every child of <path> is done.` and stop.
- Any id in its `depends_on` is not `done` -> stop with `Stopped at <id>: it depends on <ids>, which are not done.`
- Its plan at `<repo root>/<plan>` is missing -> stop with `<id> has no plan at <plan>; run /whygraph-plan <path> again.`

Print `### <id>: <title>`. The child's plan is the plan for the checks below; `TASKS_PATH` is the tasks.json's absolute path and `TASK_ID` is the child's `id`. When this run finishes, run the prompt again for the next child. The implementor refuses a `tasks.json` that git tracks.

**A plan** (the given plan, or the child's plan):

1. Confirm the file contains a line matching `^# Plan: (.+)$`. If not, respond:
   `<path> doesn't look like a WhyGraph plan (no '# Plan:' line).` and stop.
2. Look for a `## Blockers` section. If it exists **and contains at least one bullet**, refuse:
   ```
   Plan has unresolved blockers. Resolve them in the plan file first, or remove the section, then re-run.
   ```
   Print the blocker bullets verbatim under that line, then stop. Do not dispatch.
3. Look for a `## Open questions` section. If any bullet in it says `**Plan-shaping:** yes`, refuse:
   ```
   Plan has an unanswered plan-shaping question. Answer it (run the plan command again) or edit the plan.
   ```
   Print those question bullets verbatim under that line, then stop. Do not dispatch.

If validation passes, do not summarise the plan - the implementor reads it itself.

### Step 2b - Capture

Decide the `CAPTURE` value once per plan. `CAPTURE_PATH` is `.github/whygraph/capture.mjs`, relative to the workspace root.

1. With `--no-capture`, use `CAPTURE: none`.
2. If the plan has no `## UI profile` section, use `CAPTURE: none`.
3. Otherwise read `.whygraph/profiles/ui.md`. Decide from this file only, never from the plan text.
   - The file is missing: use `CAPTURE: none`. Print `Capture: off (no .whygraph/profiles/ui.md; run /whygraph-plan to write it).`
   - The file has a line that starts with `capture: unavailable`: use `CAPTURE: none`. Print that line.
   - Otherwise use `CAPTURE: node .github/whygraph/capture.mjs`.

## Step 3 - Dispatch to the implementor subagent

This prompt runs as the `whygraph-implementor` custom agent (`.github/agents/implementor.agent.md`). Its input is exactly:

```
PLAN_PATH: <absolute path to the plan>
STEP_RANGE: auto
ACCEPTANCE: inline
COMMIT_EACH_STEP: <true | false>
FIX_ATTEMPTS: <0-2>
TASKS_PATH: <absolute path to the tasks.json | none>
TASK_ID: <the child's id | none>
CAPTURE: <node .github/whygraph/capture.mjs | none>
```

With `--from-step N`, the second line is `STEP_RANGE: <N>-` instead. `ACCEPTANCE: inline` means the run walks the plan's `## Acceptance` list once its range reaches the last step, and for a split child writes that child's `status` in the tasks.json.

`CAPTURE` is the value from Step 2b. The implementor has its own system prompt covering plan parsing, step trees, Verify, capture, fix attempts and log mutation - do not inline implementation instructions here.

## Step 4 - Print the result

When the implementor returns, print its output **verbatim** to the user. Do not summarise, paraphrase, re-format, or comment. The implementor is the source of truth.

Do not edit the plan file or the tasks.json from this prompt - the implementor owns all log and status writes.

If the report has a `## Decisions worth a note` section, end by telling the user to run `/whygraph-note` once for each listed entry (this prompt has no WhyGraph tools, so it cannot record them itself).

This prompt's only job is: parse -> validate -> dispatch -> print. Do not edit other files, run other tools, or improvise.
