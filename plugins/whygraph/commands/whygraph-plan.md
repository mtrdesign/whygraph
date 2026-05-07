---
description: Plan a non-trivial code change with WhyGraph rationale + CodeGraph symbols. Asks scoping questions, then runs a fan-out/fan-in pipeline (planner → 3 parallel researchers → synthesizer) to produce a step-by-step plan.
argument-hint: <task description> [--shallow|--deep] [--no-questions] [--out <path>]
allowed-tools: [Agent, AskUserQuestion, Write, Bash]
---

# /whygraph-plan

Arguments: `$ARGUMENTS`

## Step 1 — Parse arguments

Strip and remember these flags if present (any order, anywhere in `$ARGUMENTS`):

- `--shallow` → force single-pass mode (skip fan-out)
- `--deep` → force fan-out mode (skip the heuristic)
- `--no-questions` → skip the scoping Q&A
- `--out <path>` → save the final plan to `<path>` instead of the default `.whygraph/plans/<slug>.md`. The token after `--out` is consumed as the path (not part of the task description).

Whatever remains is the **task description**. If empty after stripping, respond with:
`Usage: /whygraph-plan <task description> [--shallow|--deep] [--no-questions] [--out <path>]` and stop.

`--shallow` and `--deep` are mutually exclusive — if both passed, respond with `Pass --shallow OR --deep, not both.` and stop.

## Step 2 — Scoping Q&A (skip if `--no-questions`)

Read the task description. Decide whether it is **already specific** — that is:

- It names concrete files, symbols, or modules the change targets, AND
- It states a clear verb (add / remove / replace / refactor / migrate), AND
- It is bounded (one feature, one refactor — not "make the codebase cleaner").

If the task is already specific, **skip the questions** — go straight to Step 3.

Otherwise, surface **1–3 questions** via the AskUserQuestion tool, picking only from these axes (and only the ones that are genuinely unclear):

1. **Refactor vs feature.** "Is this a refactor (preserve behaviour) or a feature (change behaviour)?" Options: Refactor / Feature / Mixed.
2. **Scope breadth.** "How wide is the intended scope?" Options: Single file / Single module / Cross-cutting (multiple modules).
3. **Hard constraints.** "Are there hard constraints I should know up front?" Options: Backward compatibility / Performance budget / Deadline / None.

Do not invent questions outside these axes. Do not ask all three by default — only ask what's actually ambiguous given what the user wrote.

Capture the user's answers verbatim into a `SCOPING:` block to pass to the planner.

## Step 3 — Dispatch to the planner

Spawn the `whygraph-planner` subagent via the Agent tool with `subagent_type: "whygraph-planner"`. The prompt body must be:

```
TASK: <task description>
MODE: <shallow | deep | auto>
SCOPING:
- <axis>: <user answer>
- <axis>: <user answer>
(or "SCOPING: skipped" if --no-questions or no questions were needed)
```

Where `MODE` is `shallow` if `--shallow` was passed, `deep` if `--deep` was passed, otherwise `auto` (the planner will decide based on impact size).

The planner has its own system prompt and access to CodeGraph + WhyGraph MCP tools — do not inline planning instructions here.

## Step 4 — Print the result

When the planner returns, print its output **verbatim** to the user. Do not summarize, paraphrase, re-format, or comment on the plan. The planner is the source of truth.

If the planner reports that CodeGraph isn't available, surface that message verbatim and stop — don't attempt a fallback or save anything. WhyGraph planning is meaningless without the graph.

## Step 5 — Save the plan to disk

Only run this step if the planner output contains a plan (i.e. a line matching `^# Plan: (.+)$`). If no such line was found, skip the save silently — the planner returned an error or refusal, not a plan.

**5a. Resolve the save path:**

- If `--out <path>` was passed in Step 1, use that path. Resolve relative paths against the current working directory. If the path doesn't end in `.md`, append `.md`.
- Otherwise, build the default path:
  1. Extract the title from the `# Plan: <title>` line.
  2. Slugify: lowercase, replace any run of non-alphanumeric characters with a single `-`, strip leading/trailing `-`, truncate to 60 chars.
  3. Default path: `.whygraph/plans/<slug>.md` (relative to the repo root — find it via `git rev-parse --show-toplevel`).
  4. If that file already exists, append `-2`, `-3`, … until you find an unused name. Don't overwrite.

**5b. Write the file:**

- Ensure the parent directory exists with `mkdir -p <parent>` via Bash.
- Use the Write tool to save the planner's verbatim output to the resolved path.

**5c. Tell the user:** after the verbatim plan, print one final line: `Saved plan to <resolved-path>`.

This command's only job is: parse → scope → dispatch → print → save. Do not edit other files, run other tools, or improvise.
