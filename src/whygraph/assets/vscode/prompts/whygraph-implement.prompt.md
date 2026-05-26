---
description: Execute a reviewed WhyGraph plan markdown step by step. Reads the plan as a strict contract, runs Verify after each step, halts on first failure.
name: whygraph-implement
argument-hint: <plan.md> [--from-step N] [--commit-each-step] [--model <model-id>]
agent: agent
---

# /whygraph-implement — execute a reviewed WhyGraph plan

Execute a reviewed WhyGraph plan markdown step by step. Reads the plan as a strict contract, runs Verify after each step, halts on first failure, appends an Implementation log to the plan.

**Argument shape:** `<plan.md> [--from-step N] [--commit-each-step] [--model <model-id>]`

## Step 1 — Parse arguments

Strip and remember these flags if present (any order, anywhere in the user's input):

- `--from-step <N>` → integer ≥ 1 (consume the next token as `N`). Default: `auto` — the implementor picks the first step not marked `[x]` in the plan's `## Implementation log`, or `1` if no log exists.
- `--commit-each-step` → boolean flag (default `false`).
- `--model <model-id>` → optional override (consume the next token). Default: the implementor subagent's inherited model.

The first non-flag positional token is the **plan path**. If empty, respond with:
`Usage: /whygraph-implement <plan.md> [--from-step N] [--commit-each-step] [--model <model-id>]` and stop.

## Step 2 — Pre-flight validation (read-only)

1. Resolve the plan path (relative paths against the current working directory). Read the file.
   - If the file doesn't exist, respond with the error verbatim and stop.
2. Confirm the file contains a line matching `^# Plan: (.+)$`. If not, respond:
   `<path> doesn't look like a WhyGraph plan (no '# Plan:' line).` and stop.
3. Look for a `## Blockers` section. If it exists **and contains at least one bullet**, refuse:
   ```
   Plan has unresolved blockers. Resolve them in the plan file first, or remove the section, then re-run.
   ```
   Print the blocker bullets verbatim under that line, then stop. Do not dispatch.

If validation passes, do not summarise the plan — the subagent will read it itself.

## Step 3 — Dispatch to the implementor subagent

Delegate to the `whygraph-implementor` custom agent (it lives in `.github/agents/implementor.agent.md` and appears in the VS Code agents dropdown). If `--model` was passed in Step 1, hand that off as the model override.

The handoff prompt body must be exactly:

```
PLAN_PATH: <absolute path to the plan>
FROM_STEP: <integer or "auto">
COMMIT_EACH_STEP: <true | false>
```

The implementor has its own system prompt covering plan parsing, step iteration, verify cadence, and log mutation — do not inline implementation instructions here.

## Step 4 — Print the result

When the implementor returns, print its output **verbatim** to the user. Do not summarise, paraphrase, re-format, or comment. The implementor is the source of truth.

Do not edit the plan file from this prompt — the subagent owns all log mutations.

This prompt's only job is: parse → validate → dispatch → print. Do not edit other files, run other tools, or improvise.
