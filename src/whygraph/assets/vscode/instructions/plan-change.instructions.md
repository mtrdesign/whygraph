---
name: WhyGraph Plan-Change
description: When the user wants to plan a non-trivial code change — "plan how to add X", "what would it take to migrate to Y", "outline the steps to refactor Z" — surface the /whygraph-plan prompt. The planner grounds the plan in CodeGraph (structural impact) and WhyGraph (rationale, constraints, risks). Cost-gated — don't run /whygraph-plan automatically; the user opts in.
---

# Suggest /whygraph-plan for non-trivial changes

The user is gesturing at a multi-file change, feature addition, migration, or deletion that ripples. WhyGraph has a planner subagent (invoked via the `/whygraph-plan` prompt) that grounds plans in CodeGraph (structural impact) and WhyGraph (rationale, constraints, risks). It produces an ordered, verifiable plan with verbatim rationale quotes, optionally fanning out to three parallel researchers (impact / constraints+risks / prior art) for richer changes.

## What to do

1. Recognize the planning intent. Trigger phrases:
   - "Plan how to <do X>"
   - "What would it take to <change Y>?"
   - "Outline the steps to..."
   - "I'm thinking about refactoring <something concrete>"
   - "How should I approach <multi-file change>?"
   - "Design a migration for <Y>"
2. Suggest: `` `/whygraph-plan <one-line restatement of the task>` ``.
3. Mention `--deep` only if the user explicitly asks about exhaustive analysis (forces the 3-researcher fan-out path). Mention `--shallow` only if the user explicitly asks for a quick single-pass plan. Default (no flag) lets the planner choose.
4. **Do not run the prompt yourself.** Planning runs CodeGraph impact queries + rationale fetches + a planner subagent (and optionally researchers + synthesizer) — real tokens and latency. The slash command is the user's opt-in cost gate.

## When NOT to suggest /whygraph-plan

- **Trivial change** (1–3 lines, single file). Just do it.
- **The user already has a plan** and is asking you to execute. `/whygraph-plan` plans; it doesn't implement.
- **CodeGraph isn't initialised** — the planner will abort. Suggest `whygraph init` (or `codegraph init -i`) first.
- **Conceptual "how would I approach X" question** with no intent to actually change code. Answer directly.
- **Brand-new feature with no existing code to ground against** — there's nothing for the planner to read rationale from.
- **The user is asking you to *review* an existing plan** rather than draft a new one — answer directly.

## What you should NOT do

- Don't expand the suggestion into a meta-discussion of how planning works. Be concise: *"Want a grounded plan? `/whygraph-plan <restatement>`."*
- Don't draft your own plan in parallel as a "preview" — that defeats the cost gate. Either suggest `/whygraph-plan` and stop, or plan directly without invoking WhyGraph.
- Don't suggest `/whygraph-plan` repeatedly within one conversation. Once is enough.
- Don't elaborate on the `--deep` / `--shallow` flags by default — the planner's auto-mode is good enough for most calls. Only mention flags when the user explicitly asks about depth.
