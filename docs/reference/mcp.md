# MCP surface

Each project's MCP endpoint on the [portal](../portal/index.md) serves three tools, four resources, and
three prompts. That's the whole surface - deliberately narrow. WhyGraph owns "why this exists and when it changed"; graph traversal
("what's connected to what") stays with CodeGraph.

For a usage-first walkthrough of how an agent calls these mid-task, see
[Using WhyGraph](../guide/mcp-usage.md).

## Transport and endpoint

The endpoint for a project is `http://127.0.0.1:<port>/mcp/<slug>` - stateless Streamable HTTP MCP, one
endpoint per project, on the portal's port (`8765` by default). The same tools, resources and prompts
back every project; the endpoint binds the project named in the URL for the duration of each call.

- The project must be **initialized** in the portal, or the endpoint answers `409`.
- The MCP endpoint exists **only in local mode**; a [production](../deploy/production.md) portal
  answers `404` at `/mcp`. In local mode there is no authentication: the portal is loopback-only. The
  `Host` must be the loopback address and port, and a request carrying a foreign `Origin` is rejected.
- Agents are configured for you when you initialize a project - see
  [Connecting agents](../portal/agents.md). The stdio `whygraph-mcp` server of 1.x no longer exists.

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

With `qualified_name`, the symbol's file is first checked against the CodeGraph index: if it changed
since it was indexed (you edited it and have not scanned yet), the index is re-synced and the symbol
looked up again, so the evidence follows the lines as they are now. The re-sync is kept short: an
incremental `codegraph sync` only, with a time limit, and only where the `codegraph` binary is
installed (always the case in the portal). If it cannot run, fails, or leaves the file stale, the tool
still answers with the indexed range and adds `"index_stale": true` to `target`: the lines may have
drifted, so treat the evidence with care or run a scan.

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
card. Cards are cached, so a repeat call on unchanged code is a fast database read. The cache key is
the target plus a fingerprint of its evidence **and** the provider and model that wrote the card, so
switching `[rationale].provider` or `model` regenerates rather than returning the old one.

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

## Linked projects

A project [linked to a platform](../portal/platform-projects.md) has no local WhyGraph database, so
its tools blame your working tree locally and ask the platform about the **pushed** commits. The
tools, parameters and resources are the same; the results differ in three ways.

**A `platform` block.** Every tool result and resource payload of a linked project carries
`"platform": { "status": "...", "url": "..." }`: the link's status after the call, and the platform
organization's address.

| `status` | Meaning |
|---|---|
| `ok` | The platform answered (a refusal about one request, such as an unknown commit, leaves it `ok`) |
| `unreachable` | The platform did not answer |
| `revoked` | The platform refused the connection token; reconnect or remove the project |
| `removed` | The platform project, or its organization, was deleted |
| `access_lost` | The platform can no longer read the repository; its history may be stale |
| `update_required` | The platform needs a newer WhyGraph than this portal |

**A `push_status` on commits the platform did not supply.** In `whygraph_evidence_for`, a commit that
blame names but the platform cannot account for is returned from git alone (`"source": "local"`, a
null `llm_description`, no pull requests or issues) with one of:

| `push_status` | Meaning |
|---|---|
| `uncommitted` | The lines are not committed yet |
| `not_pushed` | The commit is in this checkout only: no `origin/*` ref holds it |
| `pending_scan` | Pushed to the default branch, but the platform has not scanned it yet |
| `not_on_default_branch` | Pushed, but not to the branch the platform scans |

The labels come from your `origin/*` refs as the last fetch left them; WhyGraph never fetches.
`whygraph://commit/{sha}` for a commit the platform lacks returns its git message with the same
label.

**Offline behaviour.** If the platform cannot be reached, is revoked or is gone,
`whygraph_evidence_for` still answers with your blame, every pushed commit labelled `pending_scan`
or `not_on_default_branch`, and a `platform.status` that says why. `whygraph_area_history`,
`whygraph_rationale_brief` and the resources need the platform's history and fail with a message that
names the fix.

**Other differences.** Only pushed commits, paths and symbol names are sent to the platform.
`whygraph_rationale_brief` returns a card generated and cached on the platform (shared by the whole
team), and fails with "no pushed history" while the target's lines are not on a pushed commit.
`whygraph_area_history` answers for a path only when a pushed default-branch revision tracks it.
`whygraph_evidence_for` never writes a description locally.

## Composition with CodeGraph

**This MCP surface** exposes no graph-traversal tools on purpose. The split:

| Layer | Owns |
|---|---|
| **CodeGraph** | "what is connected to what" - callers, callees, symbol resolution, type hierarchy. |
| **WhyGraph** | "why does this exist and when did it change" - evidence, rationale, history. |

For traversal mid-conversation, call CodeGraph's own tools directly - install its MCP server
alongside WhyGraph's.

This is a boundary on the MCP surface, not on WhyGraph's access to the graph. The
[Explorer](../guide/playground.md) and the [chat assistant](../guide/chat.md) both traverse it,
because they read CodeGraph's database in-process rather than over MCP.
