# WhyGraph workflow

This repository uses WhyGraph (via the `whygraph` MCP server) to ground code-change decisions in the *intent* behind existing code — purpose, constraints, tradeoffs, and risks distilled from git history, PRs, and issues. Apply these rules whenever you're working with Copilot in this repo.

## Before editing existing code

When the request is to **edit, refactor, rename, move, or delete** an existing function, class, or module — anything beyond a typo or formatter pass — call the `whygraph_rationale_brief` MCP tool first. Identify the symbol by `qualified_name` (preferred) or by `path` + `line_start` + `line_end`.

The tool returns `purpose`, `why`, `constraints[]`, `tradeoffs[]`, `risks[]`, `confidence`, `evidence_count`. Use it to inform the change:

- **Constraints are non-negotiable.** If the requested change would violate one, surface that to the user before editing.
- **Tradeoffs** explain why an obvious-looking improvement may have already been considered and rejected.
- **Risks** are flagged *before* the change, not after.
- **Low confidence (< 0.4)**: treat as a hint, not a directive — verify against the code.
- **Empty `evidence_count`**: the chunk has no meaningful history yet; proceed with judgement.

Do not dump the full brief verbatim to the user unless asked. Use it to shape the edit, then mention only the parts that affect the change.

## When the user asks "why does X exist?"

For intent questions about an existing symbol — "what is this for?", "is this still needed?", "can I delete this?" — call `whygraph_rationale_brief` and present the response **verbatim**. The grounded answer is the value; don't paraphrase or pre-answer from the code.

If the user suspects the cached rationale is stale, re-call with `force_refresh: true`.

## When planning a non-trivial change

For multi-file changes, migrations, feature additions, or anything that ripples across the codebase, use the `/whygraph-plan` prompt. It dispatches to a planner subagent that builds a CodeGraph impact set, warms the rationale cache, and produces an ordered step-by-step plan with verbatim constraint/risk quotes.

Do not draft a parallel plan yourself — that defeats the cost gate. Either invoke `/whygraph-plan` or plan inline without WhyGraph.

## When executing a reviewed plan

If the user has a WhyGraph plan markdown (typically under `.whygraph/plans/<slug>.md`) and wants to apply it, use the `/whygraph-implement` prompt. It hands off to an implementor subagent that reads the plan as a strict contract, runs Verify after each step, and halts on the first failure.

## Using CodeGraph for structural questions

This project ships a CodeGraph index (`.codegraph/`) and the `codegraph_*` MCP tools — a tree-sitter knowledge graph of every symbol, edge, and file. Reach for them on **structural** questions; use grep/read only for literal text (string contents, comments, log messages) or once a file is already open.

| Question | Tool |
|---|---|
| Where is X defined? / find a symbol by name | `codegraph_search` |
| What calls Y? / what does Y call? | `codegraph_callers` / `codegraph_callees` |
| What would break if I change Z? | `codegraph_impact` |
| Show me Y's signature / source | `codegraph_node` |

- **Trust the results** — they come from a full AST parse. Don't re-verify them with grep.
- **Don't pull large `codegraph_explore` / `codegraph_context` output into your working context** for simple questions — those return whole source sections and bloat the conversation. Use the lightweight tools above for targeted lookups, and reserve the bulk-source tools for genuine deep dives.
- **Index lag**: the watcher debounces ~500ms behind writes; don't re-query immediately after editing a file in the same turn.

If `.codegraph/` doesn't exist, the MCP server reports "not initialized" — ask the user whether to run `whygraph scan` (which builds / refreshes the index) before relying on these tools.

## What NOT to do

- Don't call `whygraph_rationale_brief` for brand-new code with no predecessor — there's no history to summarize.
- Don't call it repeatedly for the same symbol in one conversation.
- Don't treat the brief as authoritative if the code clearly diverges from what it describes — flag the divergence.
- Don't run `/whygraph-plan` automatically for trivial changes (1–3 lines, single file). Just do the edit.
- Don't grep for a symbol you could find with `codegraph_search`, or re-verify codegraph results with grep.
