---
name: plan-change
description: When the user wants to plan a non-trivial code change — "plan how to add X", "what would it take to migrate to Y", "outline the steps to refactor Z", or similar — surface the /whygraph-plan slash command. The planner grounds the plan in CodeGraph impact and WhyGraph rationale. Cost-gated — do not run /whygraph-plan automatically; the user opts in.
---

# Suggest /whygraph-plan for non-trivial changes

The user is gesturing at a multi-file change, feature addition, migration, or deletion that ripples. WhyGraph has a planner subagent (`/whygraph-plan`) that grounds plans in CodeGraph (structural impact) and WhyGraph (rationale, constraints, risks). It produces an ordered, verifiable plan with verbatim rationale quotes.

## What to do

1. Recognize the planning intent. Trigger phrases:
   - "Plan how to <do X>"
   - "What would it take to <change Y>?"
   - "Outline the steps to..."
   - "I'm thinking about refactoring <something concrete>"
   - "How should I approach <multi-file change>?"
2. Suggest: `` `/whygraph-plan <one-line restatement of the task>` ``. Mention `--deep` only if the user explicitly asks about exhaustive analysis (and note it's reserved for v2 fan-out planning).
3. **Do not run the command yourself.** Planning runs `codegraph_impact` plus rationale fetches plus a planner subagent — real tokens and latency. The slash command is the user's opt-in cost gate.

## When NOT to suggest /whygraph-plan

- **Trivial change** (1-3 lines, single file). Just do it.
- **The user already has a plan** and is asking you to execute. `/whygraph-plan` plans; it doesn't implement.
- **CodeGraph isn't initialized** — the planner will abort. Suggest CodeGraph setup first instead.
- **Conceptual "how would I approach X" question** with no intent to actually change code. Answer directly.
- **Brand-new feature with no existing code to ground against** — there's nothing for the planner to read rationale from.

## What you should NOT do

- Don't expand the suggestion into a meta-discussion of how planning works. Be concise: *"Want a grounded plan? `/whygraph-plan <restatement>`."*
- Don't draft your own plan in parallel as a "preview" — that defeats the cost gate. Either suggest `/whygraph-plan` and stop, or plan directly without invoking WhyGraph.
- Don't suggest `/whygraph-plan` repeatedly within one conversation. Once is enough.
