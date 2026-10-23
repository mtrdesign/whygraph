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

Both return compact items (no bodies or comments) unless you pass `detail="full"`, which adds
commit bodies to the items and the pull request and issue prose once, in top-level maps keyed by
number; see [Evidence items](../reference/mcp.md#evidence-items).

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

The `whygraph` server has **no graph-traversal tools** - no callers, no callees, no symbol search.
That's CodeGraph's job, on purpose, and you do not install it: the portal runs CodeGraph's own MCP
server for you and serves it as a second server named `codegraph`, with four read-only tools
(`codegraph_explore`, `codegraph_search`, `codegraph_node`, `codegraph_impact`). When your assistant
needs "who calls this?", it asks `codegraph`; `codegraph_node` shows a symbol's callers and callees,
and `codegraph_impact` the transitive dependents. WhyGraph stays focused on why the code exists and how
it got here. The tools are listed under [The CodeGraph endpoint](../reference/mcp.md#the-codegraph-endpoint).

## Freshness { #freshness }

CodeGraph's tools read an index of the repository, and an editor saves files faster than any scan. So
the portal checks the index before it answers each `codegraph_*` call:

- It computes a **fingerprint** of the checkout - `HEAD`, the git status and the size and modification
  time of each changed file - in about 0.1 s even on large repositories. If it matches the fingerprint
  of the last refresh, the call goes straight through.
- Otherwise it runs **one** refresh (`codegraph sync`) and the call waits for it. Calls that arrive
  meanwhile join that refresh, and at most one more is queued if files change again during it. A refresh
  takes roughly 0.1 s per changed file plus a fixed cost, so a few edited files cost under a second or
  two.
- The wait is bounded at **45 seconds**. Past that the call fails with an error that begins
  `[WhyGraph] The code index is still updating` and says how many seconds to wait. The refresh keeps
  running, so calling the same tool again after that usually answers at once. A call never reads an
  index that is being changed, and never gets stale results.
- A scan's own index step takes the same per-project lock, so a call during a rebuild waits for it
  within the same 45 seconds.

Commits are normally already indexed by the git hook scan, so the per-call refresh mostly covers files
you have edited but not committed. If a refresh fails three times in ten minutes, refreshes for that
project pause for five minutes and calls say the index could not be refreshed. Files
that CodeGraph indexes but git ignores (`includeIgnored` in `codegraph.json`) do not move the
fingerprint and are picked up by the next scan.

### Client timeouts

A call that waits for the index can take tens of seconds, and agents differ in how long they let a tool
run. What is known, with its source:

| Agent | Tool timeout | Notes |
|---|---|---|
| Claude Code | No short limit (documented: a 5-minute idle window for HTTP, reset by progress) | WhyGraph sends progress notifications every 3 seconds while a call waits |
| Cursor | Reported, not documented by Cursor: about 60 minutes in the IDE, 60 seconds hard in the agent protocol path, where progress is ignored | The 45-second wait was chosen to stay under 60 seconds |
| VS Code / Copilot | Not documented | |
| Codex | Documented `tool_timeout_sec` default 60 seconds; its current source uses 300 seconds | The portal writes `tool_timeout_sec = 120` on both Codex entries |

If your client cuts calls off earlier than 45 seconds, the refresh still finishes in the background and
the retry succeeds.
