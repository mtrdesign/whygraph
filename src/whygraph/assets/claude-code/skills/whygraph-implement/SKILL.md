---
name: whygraph-implement
description: Execute a reviewed WhyGraph plan (or a split's tasks.json) one step per fresh implementor at the step's model, with a cached-card review gate after each step and a separate acceptance run at the end. Each run halts on the first failure and appends to the plan's Implementation log.
argument-hint: <plan.md | tasks.json> [--from-step N] [--step N] [--commit-each-step] [--fix-attempts N] [--model sonnet|opus|haiku] [--no-review]
allowed-tools: Agent, Read, mcp__whygraph__whygraph_propose_note
disable-model-invocation: true
---

# /whygraph-implement

Arguments: `$ARGUMENTS`

## Step 1 - Parse arguments

Strip and remember these flags if present (any order, anywhere in `$ARGUMENTS`):

- `--from-step <N>` -> integer >= 1 (consume the next token as `N`). Run step N through the last step. Step N runs even if the log marks it done.
- `--step <N>` -> integer >= 1 (consume the next token as `N`). Run only step N, even if the log marks it done.
- `--commit-each-step` -> boolean flag (default `false`).
- `--fix-attempts <N>` -> integer 0-2 (consume the next token; default `0`, values above 2 become 2). On a failed Verify the implementor makes up to `N` bounded fixes of the same step's files, re-running Verify each time, before it halts.
- `--model <sonnet|opus|haiku>` -> optional (consume the next token). It overrides every step's `**Model:**` tag.
- `--no-review` -> skip the review gate after each step (Step 3b).

The first non-flag positional token is the **path**: a plan (`.md`) or a split's `.tasks.json`. If it is empty, respond with:
`Usage: /whygraph-implement <plan.md | tasks.json> [--from-step N] [--step N] [--commit-each-step] [--fix-attempts N] [--model sonnet|opus|haiku] [--no-review]` and stop.

- If both `--from-step` and `--step` are passed, respond with `Pass --from-step OR --step, not both.` and stop.
- If the path ends in `.tasks.json` and `--from-step` or `--step` is passed, respond with `--from-step and --step take a plan; pass the child's plan file instead of the tasks.json.` and stop.

## Step 2 - Pre-flight validation (use Read only)

Resolve the path against the current working directory and make it absolute. `Read` it. If the file does not exist, respond with the error verbatim and stop.

**A plan.**

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

**A tasks.json.** Check each rule below. On a failed check, respond with `Refusing <path>: <reason>.` and stop.

1. The path ends in `/.whygraph/plans/<stem>.tasks.json`. The repo root is the directory that holds `.whygraph/`.
2. The file parses as JSON and has `version: 1`.
3. `children` has 1 to 6 entries. Each `id` is one of `t1`-`t6`, and no `id` repeats.
4. Every `plan` matches `^\.whygraph/plans/[a-z0-9-]+-t[1-6]\.md$`.
5. Every `depends_on` entry names an existing `id`, and the dependencies have no cycle.

The "not tracked by git" check is the implementor's: it refuses a tracked `TASKS_PATH` in its first run. Each child's plan gets the plan checks above when its turn comes (Step 3d).

If validation passes, do not summarise the plan - each subagent reads it itself.

## Step 3 - Run the plan one step at a time

1. From the plan, collect the step numbers under `## Steps` (lines `<N>. **<name>**`).
2. For each step, take the first word of its `**Model:**` line, lowercased. Anything other than `haiku`, `sonnet` or `opus`, or no `**Model:**` line, becomes `sonnet`. `--model` replaces every step's value.
3. Pick the steps to run:
   - `--step N` -> only step N.
   - `--from-step N` -> step N through the last step.
   - Otherwise -> the first step not marked `- [x] Step <N>:` in the plan's `## Implementation log` (step 1 when there is no log) through the last step. When every step is marked, print `All steps are already marked done.` and go to Step 3c.
   - An `N` that is not a step number: respond with `<path> has no step <N>.` and stop.
4. For each step `N` in order, spawn the `whygraph-implementor` subagent via the Agent tool with `subagent_type: "whygraph-implementor"` and the Agent tool's `model` set to the step's value. The prompt body must be exactly:

   ```
   PLAN_PATH: <absolute path to the plan>
   STEP_RANGE: <N>
   ACCEPTANCE: separate
   COMMIT_EACH_STEP: <true | false>
   FIX_ATTEMPTS: <0-2>
   TASKS_PATH: <absolute path to the tasks.json | none>
   TASK_ID: <the child's id | none>
   CAPTURE: none
   ```

   The implementor has its own system prompt covering plan parsing, step trees, Verify, fix attempts and log mutation. Do not inline implementation instructions here.
5. Read the report's `**Status:**` line.
   - `complete` and not the last step -> print one line: `Step <N>: complete (<t0> <t1>)`, with the trees from the report's `- Step <N>: <t0> <t1>` line under `**Step trees:**`.
   - `complete` on the plan's last step -> print the report in full.
   - Anything else (`halted at step <N>`, a refusal) -> print the report in full, run Step 5 on it, and stop.
6. Run Step 5 on the report, then Step 3b, then the next step.

### Step 3b - Review gate (skip with `--no-review`)

After a `complete` step, check the step's diff against the cached rationale cards.

1. Take `<t0>` and `<t1>` from the report's `- Step <N>: <t0> <t1>` line under `**Step trees:**`. If the line is missing, print `Step <N>: no step trees in the report; review gate skipped.` and go on.
2. Spawn the `whygraph-reviewer` subagent via the Agent tool with `subagent_type: "whygraph-reviewer"`. The prompt body must be exactly:

   ```
   DIFF: <t0> <t1>
   GENERATE: false
   ```

3. If any verdict is `**Constraint at risk:**` or `**Risk triggered:**`, print the review in full. Otherwise print one line: `Step <N>: review found no recorded constraint or risk at stake.`
4. On a `**Constraint at risk:**` verdict, stop with:
   `Halted after step <N>: the review found a constraint at risk. Fix it, then run /whygraph-implement <plan> --from-step <N>.`
   A `**Risk triggered:**` verdict alone does not stop the loop.

### Step 3c - Acceptance

Run this only when the loop ran the plan's last step and it completed (with its gate passed), or when Step 3 found every step already marked done.

Spawn the `whygraph-implementor` once more, with the Step 3 prompt and `STEP_RANGE: acceptance` in place of `STEP_RANGE: <N>`. Set the Agent tool's `model` only when `--model` was passed. Only this run walks the plan's `## Acceptance` list and, for a split child, writes the child's `status` in the tasks.json. Print its report in full, then run Step 5 on it.

### Step 3d - A tasks.json

Run the children one at a time, even within a wave, in ascending `wave` order and then `id` order. For each child:

1. If its `status` is `done`, skip it.
2. If any id in its `depends_on` is not `done`, stop with `Stopped at <id>: it depends on <ids>, which are not done.`
3. `Read` its plan at `<repo root>/<plan>`. If the file is missing, stop with `<id> has no plan at <plan>; run /whygraph-plan again.` Run the plan checks of Step 2 on it.
4. Run Steps 3, 3b and 3c for that plan, with `TASKS_PATH` set to the tasks.json's absolute path and `TASK_ID` to the child's `id`. A halt stops the whole run.
5. `Read` the tasks.json again, because the acceptance run wrote the child's `status`.

## Step 4 - Print rules

Print each report you print in full **verbatim**. Do not summarise, paraphrase, re-format or comment. The subagent is the source of truth.

Do not edit the plan file or the tasks.json from this command - the implementor owns all log and status writes.

## Step 5 - Propose the listed decisions

If an implementor report has a `## Decisions worth a note` section, call the MCP tool `whygraph_propose_note` once per listed entry: `kind` and `body` from the entry; for the anchor, a `qualified_name`, or `path` (with `line_start` and `line_end` when the entry gives lines). Then print, for each, the note id and its review link (the portal page path the tool returns). A tool error: print it verbatim and go on to the next entry. With no such section, do nothing here. Do this once per report.

This command's only job is: parse -> validate -> one implementor per step -> review gate -> acceptance -> print -> propose the listed decisions. Do not edit files, run other tools, or improvise.
