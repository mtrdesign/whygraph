# MCP surface

`whygraph-mcp` registers three tools, four resources, and three prompts. That's the whole surface -
deliberately narrow. WhyGraph owns "why this exists and when it changed"; graph traversal
("what's connected to what") stays with CodeGraph.

For a usage-first walkthrough of how an agent calls these mid-task, see
[Using WhyGraph](../guide/mcp-usage.md).

## Tools

### `whygraph_evidence_for`

Historical evidence - commits, PRs, and closing issues - for a chunk of code. Line-blame-driven and
anchored to HEAD.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | str | - | Source file path, relative to the repo root. |
| `line_start` | int | - | First line of the chunk (1-indexed, inclusive). |
| `line_end` | int | - | Last line of the chunk (1-indexed, inclusive). |
| `qualified_name` | str | - | Fully-qualified symbol name. Use instead of `path`/lines when you know the symbol. CodeGraph resolves it to a file/line range. |
| `limit` | int | `20` | Cap on the number of commits returned. |

Returns `{ "target": {...}, "evidence": [ { "commit", "pull_requests", "issues", "source" }, ... ] }`.

### `whygraph_area_history`

Every commit that touched a file path - or any path it was renamed from. Where `evidence_for` is
line-blame-driven, `area_history` reaches commits for code that's since been deleted, moved, or
fully rewritten.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | str | *required* | The file path, as it appears at HEAD (or any commit - the rename chain is bidirectional). |
| `limit` | int | `20` | Cap on commits returned, newest first. |
| `include_renames` | bool | `true` | Walk the `renamed_from` chain to include commits that touched historical names. |

Returns `{ "path", "include_renames", "evidence": [...] }`, using the same evidence shape as
`whygraph_evidence_for`.

### `whygraph_rationale_brief`

Generate a structured rationale card explaining why a chunk of code exists. It gathers the evidence,
optionally enriches it with CodeGraph symbol context, and asks the configured LLM to synthesize the
card. Cards are cached, so a repeat call on unchanged code is a fast database read.

| Parameter | Type | Description |
|---|---|---|
| `path` | str | Source file path, relative to the repo root. |
| `line_start` | int | First line of the chunk. |
| `line_end` | int | Last line of the chunk. |
| `qualified_name` | str | Fully-qualified symbol name, instead of `path`/lines. |

Returns the card:

```json
{
  "target": { "...": "..." },
  "purpose": "...",
  "why": "...",
  "constraints": ["..."],
  "tradeoffs": ["..."],
  "risks": ["..."],
  "model": "claude-opus-4-7",
  "provider": "anthropic",
  "cached_at": "2026-06-11T12:00:00Z",
  "evidence_count": { "commits": 8, "prs": 3, "issues": 2 }
}
```

The card carries exactly five narrative fields - **purpose, why, constraints, tradeoffs, risks** -
plus provenance (`model`, `provider`, `cached_at`) and an `evidence_count` summary.

!!! note "Calls the LLM on a cache miss"
    On a miss, this hits the configured provider and may take several seconds. A hit is sub-second.
    Generation needs a credential - see [Configuration](configuration.md).

## Resources

Read-only, JSON, addressed by URI.

| URI | Name | Description |
|---|---|---|
| `whygraph://commit/{sha}` | `whygraph_commit` | A scanned commit and the pull requests that contain it (closing issues not inlined). |
| `whygraph://pr/{number}` | `whygraph_pull_request` | A pull request and the issues it closes. Includes full `commit_titles` and `comments`. |
| `whygraph://issue/{number}` | `whygraph_issue` | An issue and the pull requests that close it. |
| `whygraph://repo/overview` | `whygraph_repo_overview` | Repo-level summary: row counts, commit date range, scan freshness, LLM-description coverage, top contributors. |

## Prompts

Orchestration recipes that wire the tools into a workflow.

| Name | Title | Arguments | What it does |
|---|---|---|---|
| `whygraph_pre_edit_brief` | Pre-edit brief | `path` / `line_start` / `line_end` / `qualified_name` | Before you edit, gather rationale and history so the edit respects constraints and avoids known risks. |
| `whygraph_why_was_this_written` | Why was this written? | `path` / `line_start` / `line_end` / `qualified_name` | Recover the original intent behind a chunk of code from its commits, PRs, and closing issues. |
| `whygraph_triage_commit` | Triage a commit | `sha` | Summarize what one commit did and why, using its linked PR and closing issues. |

## Composition with CodeGraph

WhyGraph exposes no graph-traversal tools on purpose. The split:

| Layer | Owns |
|---|---|
| **CodeGraph** | "what is connected to what" - callers, callees, symbol resolution, type hierarchy. |
| **WhyGraph** | "why does this exist and when did it change" - evidence, rationale, history. |

For traversal mid-conversation, call CodeGraph's own tools directly.
