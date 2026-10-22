# Using WhyGraph (MCP)

Once your editor is connected to the portal, WhyGraph works through MCP - your assistant calls its tools mid-task, the
way it would any other tool. This page shows what's available and when to reach for each. For exact
signatures, see the [MCP reference](../reference/mcp.md).

## The tools

Three tools cover the two questions - evidence and rationale.

**Reaching for history?** Use the evidence tools:

- `whygraph_evidence_for(path, line_start, line_end | qualified_name, limit=10)` - the commits, PRs,
  and issues behind a specific range or symbol. Line-blame-driven, anchored to HEAD.
- `whygraph_area_history(path, limit=10, include_renames=True)` - every commit that touched a file or
  its rename predecessors. Use it when the code moved, got deleted, or was rewritten and blame comes
  up short.

Both return compact items (no bodies or comments) unless you pass `detail="full"`; see
[Evidence items](../reference/mcp.md#evidence-items).

**Reaching for the *why*?** Use the rationale tool:

- `whygraph_rationale_brief(path, line_start, line_end | qualified_name)` - a structured card:
  purpose, why, constraints, tradeoffs, risks. Cached, so a repeat call is instant.
  `cached_only=True` reads the cache and never generates: a miss is `{"status": "not_cached"}`, at no
  LLM cost.

A typical flow: before editing a function, your assistant calls `whygraph_rationale_brief` to ground
itself in intent. If the card comes back thin, it falls back to `whygraph_evidence_for` for the raw
history.

## Checking a diff against the rationale

In Claude Code, `/whygraph-review [<base>] [--generate]` runs the same tools over a diff: CodeGraph's
`codegraph_node` finds the symbol around each hunk, `whygraph_rationale_brief` with `cached_only=True`
reads its card, and the reviewer reports per hunk whether a constraint is at risk or a risk is
triggered, quoting the card verbatim. Without `--generate` it spends no LLM budget. On a project linked
to a platform it asks for no cards unless you pass `--generate`, because the platform does not honour
`cached_only`. See [Reviewing a diff](../portal/agents.md#whygraph-review) for what it reviews and its
limits.

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
  This is the "what did commit X do" path.

## When the project is linked to a platform

If your checkout is [linked to a platform](../portal/platform-projects.md), the tools work the same,
and an assistant should read three extra things in each result:

- **`platform.status`.** `ok` means the history came from the platform. Anything else (`unreachable`,
  `revoked`, `removed`, `access_lost`, `update_required`) means the history may be missing or old, and
  the user can fix it from the project's card in the portal.
- **`push_status` on a commit.** A commit labelled `uncommitted`, `not_pushed`, `pending_scan` or
  `not_on_default_branch` is the user's own recent work, or work the platform has not seen yet. It has
  an author, a date and a subject from git, but no description, pull request or issue. Treat it as
  "recent, not explained yet", not as "no reason exists".
- **Offline.** `whygraph_evidence_for` still returns blame when the platform is down. The rationale
  tool and area history do not: they report why.

See [Linked projects](../reference/mcp.md#linked-projects) for every value.

## What WhyGraph won't do

WhyGraph has **no graph-traversal tools** - no callers, no callees, no symbol search. That's
CodeGraph's job, on purpose. When your assistant needs "who calls this?", it asks CodeGraph directly.
WhyGraph stays focused on why the code exists and how it got here.
