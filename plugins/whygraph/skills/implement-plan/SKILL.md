---
name: implement-plan
description: When the user has a reviewed WhyGraph plan markdown file and wants to execute it — "implement that plan", "execute the plan at <path>", "run through the steps", "carry out the plan", "let's apply the plan" — surface the /whygraph-implement slash command. Cost-gated — do not run /whygraph-implement automatically; the user opts in.
---

# Suggest /whygraph-implement for executing a reviewed plan

The user has a plan markdown (typically under `.whygraph/plans/<slug>.md`) that they've reviewed and now want to apply step-by-step. WhyGraph has an implementor subagent (`/whygraph-implement`) that reads the plan as a strict contract, applies each step in order, runs the per-step Verify, halts on the first failure, and logs progress back into the plan. It pins Sonnet by default — fast and cheap for executing a well-specified plan.

## What to do

1. Recognize the implementation intent. Trigger phrases:
   - "Implement the plan at `<path>`"
   - "Run through the plan"
   - "Carry out the steps"
   - "Apply the plan"
   - "Execute `<plan.md>`"
   - "Let's implement what we planned"
2. Suggest: `` `/whygraph-implement <plan-path>` ``. Use the path the user named, or `.whygraph/plans/<slug>.md` if they referred to "the plan" generically and only one fresh plan exists.
3. Mention `--from-step N` only if the user said something like "pick up where we left off" or "skip the first few" — the default auto-resumes from the first uncompleted step.
4. Mention `--commit-each-step` only if the user asked for clean commit history per step — most users review the diff and commit themselves.
5. Mention `--model opus` only if the user explicitly says the plan is high-stakes or the steps are unusually subtle. Default Sonnet handles routine edits.
6. **Do not run the command yourself.** The implementor edits files and (optionally) commits — real side effects. The slash command is the user's opt-in cost gate.

## When NOT to suggest /whygraph-implement

- **No plan markdown exists.** If the user wants to plan first, suggest `/whygraph-plan` instead.
- **The "plan" is informal** (a chat-message bullet list, not a `# Plan:` markdown file with the standard schema). The implementor needs the structured format — suggest the user run `/whygraph-plan` to produce one.
- **Trivial change** (1–3 lines, single file). Just do the edit directly.
- **The user is asking you to *review* the plan** rather than execute it. Read it and discuss; don't dispatch.
- **The plan has unresolved blockers** (`## Blockers` section with bullets). Tell the user the implementor will refuse — they need to resolve the blockers in the plan first.
- **The user already invoked `/whygraph-implement` recently in this conversation.** Once is enough.

## What you should NOT do

- Don't expand the suggestion into a meta-discussion of how the implementor works. Be concise: *"Plan ready? `/whygraph-implement <path>`."*
- Don't draft your own implementation in parallel as a "preview" — that defeats the cost gate. Either suggest the slash command and stop, or do the edits directly without invoking the implementor.
- Don't suggest `/whygraph-implement` repeatedly within one conversation. Once is enough.
- Don't elaborate on the `--from-step` / `--commit-each-step` / `--model` flags by default — the defaults are good for most calls. Only mention flags when the user's wording calls for them.
