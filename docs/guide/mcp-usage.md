# Using WhyGraph (MCP)

Once your editor is wired, WhyGraph works through MCP - your assistant calls its tools mid-task, the
way it would any other tool. This page shows what's available and when to reach for each. For exact
signatures, see the [MCP reference](../reference/mcp.md).

## The tools

Three tools cover the two questions - evidence and rationale.

**Reaching for history?** Use the evidence tools:

- `whygraph_evidence_for(path, line_start, line_end | qualified_name, limit=20)` - the commits, PRs,
  and issues behind a specific range or symbol. Line-blame-driven, anchored to HEAD.
- `whygraph_area_history(path, limit=20, include_renames=True)` - every commit that touched a file or
  its rename predecessors. Use it when the code moved, got deleted, or was rewritten and blame comes
  up short.

**Reaching for the *why*?** Use the rationale tool:

- `whygraph_rationale_brief(path, line_start, line_end | qualified_name)` - a structured card:
  purpose, why, constraints, tradeoffs, risks. Cached, so a repeat call is instant.

A typical flow: before editing a function, your assistant calls `whygraph_rationale_brief` to ground
itself in intent. If the card comes back thin, it falls back to `whygraph_evidence_for` for the raw
history.

## The resources

Read-only JSON, addressed by URI - handy when you already know the commit, PR, or issue:

- `whygraph://commit/{sha}` - a commit and the PRs that contain it.
- `whygraph://pr/{number}` - a PR and the issues it closes.
- `whygraph://issue/{number}` - an issue and the PRs that close it.
- `whygraph://repo/overview` - repo-level summary: counts, scan freshness, coverage, top contributors.

## The prompts

Prompts are ready-made recipes that wire the tools into a workflow:

- `whygraph_pre_edit_brief` - gather rationale and history before you edit, so the change respects
  constraints and avoids known risks.
- `whygraph_why_was_this_written` - recover the original intent behind a chunk of code.
- `whygraph_triage_commit` - summarize what one commit did and why, from its PR and closing issues.

## What WhyGraph won't do

WhyGraph has **no graph-traversal tools** - no callers, no callees, no symbol search. That's
CodeGraph's job, on purpose. When your assistant needs "who calls this?", it asks CodeGraph directly.
WhyGraph stays focused on why the code exists and how it got here.
