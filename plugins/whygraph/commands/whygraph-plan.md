---
description: Plan a non-trivial code change with WhyGraph — uses CodeGraph for structural impact and WhyGraph rationale for intent/constraints/risks, then produces a step-by-step implementation plan.
argument-hint: <task description> [--deep]
---

# /whygraph-plan

Arguments: `$ARGUMENTS`

Parse them as `<task> [--deep]`:
- Strip a trailing `--deep` flag if present and remember it.
- Everything else is the task description.
- If the remaining task is empty, respond with: `Usage: /whygraph-plan <task description> [--deep]` and stop.

If `--deep` was passed, prepend this notice to your response and continue with the single-pass flow:

> Note: `--deep` (fan-out planning) is reserved for v2 and not yet implemented. Running single-pass.

## What to do

Spawn the `whygraph-planner` subagent via the Agent tool with `subagent_type: "whygraph-planner"` and the task description as the prompt. The subagent has its own system prompt and access to CodeGraph + WhyGraph MCP tools — do not inline planning instructions here.

When the subagent returns, print its output **verbatim** to the user. Do not summarize, paraphrase, re-format, or comment on the plan. The planner is the source of truth.

If the subagent reports that CodeGraph isn't available, surface that message verbatim and stop — don't attempt a fallback plan. WhyGraph planning is meaningless without the graph.

Do not edit files or call other tools. This command's only job is to dispatch to the planner.
