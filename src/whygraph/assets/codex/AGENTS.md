# WhyGraph workflow

This repository uses WhyGraph (via the `whygraph` MCP server) to ground code-change decisions in the *intent* behind existing code — purpose, constraints, tradeoffs, and risks distilled from git history, PRs, and issues. Apply these rules whenever you're working in this repo.

## Before editing existing code

When the request is to **edit, refactor, rename, move, or delete** an existing function, class, or module — anything beyond a typo or formatter pass — call the `whygraph_rationale_brief` MCP tool first. Identify the symbol by `qualified_name` (preferred) or by `path` + `line_start` + `line_end`.

The tool returns `purpose`, `why`, `constraints[]`, `tradeoffs[]`, `risks[]`, `confidence`, `evidence_count`. Use it to inform the change:

- **Constraints are non-negotiable.** If the requested change would violate one, surface that to the user before editing.
- **Tradeoffs** explain why an obvious-looking improvement may have already been considered and rejected.
- **Risks** are flagged *before* the change, not after.
- **Low confidence (< 0.4)**: treat as a hint, not a directive — verify against the code.
- **Empty `evidence_count`**: the chunk has no meaningful history yet; proceed with judgement.

If the tool returns `isError: true`, surface the error verbatim and proceed with your own judgment after flagging the cause to the user.

Don't dump the full brief verbatim to the user unless asked. Use it to shape the edit, then mention only the parts that affect the change.

**Skip when**: adding entirely new code with no predecessor, trivial edits (typos, whitespace, formatter output), or you've already fetched the brief for the same symbol earlier in this session.

## When the user asks "why does X exist?"

For intent questions about an existing symbol — "what is this for?", "is this still needed?", "can I delete this?" — call `whygraph_rationale_brief` and present the response **verbatim**. The grounded answer is the value; don't paraphrase or pre-answer from the code.

If the user suspects the cached rationale is stale, re-call with `force_refresh: true`.

The companion tool `whygraph_evidence_for` returns the raw commits / PRs / issues backing a card — call it only when the brief looks wrong and you want to verify the source.

## Planning a non-trivial change

For multi-file changes, migrations, feature additions, or anything that ripples across the codebase, hand off to the `whygraph-planner` subagent — invoke via `/agent whygraph-planner` (or natural-language delegation). The planner builds a CodeGraph impact set, warms the rationale cache, and produces an ordered step-by-step plan with verbatim constraint/risk quotes. For richer changes it fans out to three researchers (impact, constraints+risks, prior art) and a synthesizer.

Pass the task description in the planner's expected prompt shape:

```
TASK: <task description>
MODE: <shallow | deep | auto>
SCOPING: skipped       # or "- <axis>: <answer>" lines
```

Don't draft a parallel plan yourself — that defeats the cost gate. Either invoke `whygraph-planner` or plan inline without WhyGraph.

**Skip when**: trivial change (1–3 lines, single file), the user already has a plan and wants execution, or CodeGraph isn't initialised in the project.

## Executing a reviewed plan

If the user has a WhyGraph plan markdown (typically under `.whygraph/plans/<slug>.md`) and wants to apply it, hand off to the `whygraph-implementor` subagent — `/agent whygraph-implementor`. It reads the plan as a strict contract, applies each step in order, runs Verify after each step, halts on the first failure, and logs progress back into the plan.

Pass the implementor's expected prompt shape:

```
PLAN_PATH: <absolute path>
FROM_STEP: <integer or "auto">
COMMIT_EACH_STEP: <true | false>
```

**Skip when**: no plan markdown exists, the "plan" is an informal chat-message bullet list (not a `# Plan:` markdown file with the standard schema), the change is trivial, the user is asking you to *review* the plan rather than execute it, or the plan has unresolved blockers.

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
- Don't dispatch `whygraph-planner` or `whygraph-implementor` for trivial changes.
- Don't draft your own plan or implementation in parallel "as a preview" while waiting on a subagent — pick one path.
- Don't grep for a symbol you could find with `codegraph_search`, or re-verify codegraph results with grep.
