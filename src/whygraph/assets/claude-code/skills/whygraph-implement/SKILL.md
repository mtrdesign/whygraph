---
name: whygraph-implement
description: Execute a reviewed WhyGraph plan markdown step by step. Reads the plan as a strict contract, runs Verify after each step, halts on first failure, appends an Implementation log to the plan.
argument-hint: <plan.md> [--from-step N] [--commit-each-step] [--model sonnet|opus|haiku]
allowed-tools: [Agent, Read]
disable-model-invocation: true
---

# /whygraph-implement

Arguments: `$ARGUMENTS`

## Step 1 — Parse arguments

Strip and remember these flags if present (any order, anywhere in `$ARGUMENTS`):

- `--from-step <N>` → integer ≥ 1 (consume the next token as `N`). Default: `auto` — implementor picks the first step not marked `[x]` in the plan's `## Implementation log`, or `1` if no log exists.
- `--commit-each-step` → boolean flag (default `false`).
- `--model <sonnet|opus|haiku>` → optional override (consume the next token). Default: subagent's pinned model (`sonnet`).

The first non-flag positional token is the **plan path**. If empty, respond with:
`Usage: /whygraph-implement <plan.md> [--from-step N] [--commit-each-step] [--model sonnet|opus|haiku]` and stop.

## Step 2 — Pre-flight validation (use Read only)

1. Resolve the plan path (relative paths against the current working directory). Read the file with the Read tool.
   - If the file doesn't exist, respond with the error verbatim and stop.
2. Confirm the file contains a line matching `^# Plan: (.+)$`. If not, respond:
   `<path> doesn't look like a WhyGraph plan (no '# Plan:' line).` and stop.
3. Look for a `## Blockers` section. If it exists **and contains at least one bullet**, refuse:
   ```
   Plan has unresolved blockers. Resolve them in the plan file first, or remove the section, then re-run.
   ```
   Print the blocker bullets verbatim under that line, then stop. Do not dispatch.

If validation passes, do not summarise the plan — the subagent will read it itself.

## Step 3 — Dispatch to the implementor

Spawn the `whygraph-implementor` subagent via the Agent tool with `subagent_type: "whygraph-implementor"`. If `--model` was passed in Step 1, set the Agent tool's `model` parameter to that value (it overrides the subagent's pinned default).

The prompt body must be exactly:

```
PLAN_PATH: <absolute path to the plan>
FROM_STEP: <integer or "auto">
COMMIT_EACH_STEP: <true | false>
```

The implementor has its own system prompt covering plan parsing, step iteration, verify cadence, and log mutation — do not inline implementation instructions here.

## Step 4 — Print the result

When the implementor returns, print its output **verbatim** to the user. Do not summarise, paraphrase, re-format, or comment. The implementor is the source of truth.

Do not edit the plan file from this command — the subagent owns all log mutations.

This command's only job is: parse → validate → dispatch → print. Do not edit other files, run other tools, or improvise.
