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
| `limit` | int | `10` | Cap on the number of commits returned, 1 to 100. |
| `detail` | `"compact"` \| `"full"` | `"compact"` | How much of each item to return - see [Evidence items](#evidence-items). |
| `describe` | bool | `true` | Generate missing commit descriptions (bounded, see below). `false` never calls an LLM. |

Returns `{ "target": {...}, "evidence": [ ... ], "describing": 0 }`, newest first. The items are
described under [Evidence items](#evidence-items); `lines` is a list of `[start, end]` working-tree
ranges the commit owns within the queried range, set on `blame` items and `null` otherwise.

With `qualified_name`, the symbol's file is first checked against the CodeGraph index: if it changed
since it was indexed (you edited it and have not scanned yet), the index is re-synced and the symbol
looked up again, so the evidence follows the lines as they are now. The re-sync is kept short: an
incremental `codegraph sync` only, with a time limit, and only where the `codegraph` binary is
installed (always the case in the portal). If it cannot run, fails, or leaves the file stale, the tool
still answers with the indexed range and adds `"index_stale": true` to `target`: the lines may have
drifted, so treat the evidence with care or run a scan.

**Missing descriptions are bounded.** A project scanned without descriptions (or a commit added
since) has commits with a null `llm_description`. Each call describes at most **5** of them, newest
first, and starts none after **20 seconds**; `describing` counts the ones it left, which a later
call picks up. Pass `describe=false` to skip the LLM entirely (`describing` then counts every
undescribed commit).

### Evidence items

`whygraph_evidence_for` and `whygraph_area_history` return the same item shape.

**`detail="compact"`** (the default) leaves out every body and comment:

```json
{
  "sha": "4f1c...",
  "llm_description": "Adds a retry around the token refresh ...",
  "subject": "fix: retry refresh",
  "author": "Jane Doe",
  "date": "2026-02-01T10:00:00+00:00",
  "lines": [[120, 141]],
  "source": "blame",
  "pull_requests": [
    { "number": 847, "title": "Retry token refresh", "html_url": "https://github.com/...", "resource": "whygraph://pr/847" }
  ],
  "issues": [
    { "number": 812, "title": "Sessions drop", "html_url": "https://github.com/...", "resource": "whygraph://issue/812" }
  ]
}
```

Read a PR or issue in full through its `resource` (see [Resources](#resources)).

**`detail="full"`** adds the prose, grouped under `text` because it is repository and GitHub content,
never instructions: `text.body` on the commit and on each issue, and on each pull request
`text.body`, `text.comments` (the first `[rationale].pr_discussion_max_comments`, each cut to
`pr_comment_max_chars`) and `commit_titles` (the first `pr_roster_max_commits`). A pull request whose
lists were cut carries `"omitted": { "comments": n, "commit_titles": m }`.

`source` says how the commit was found: `blame`, `pr-origin`, `blame-walked`, `predecessor-blame` or
`area`. A [linked project](#linked-projects) may add `push_status`.

### `whygraph_area_history`

Every commit that touched a file path - or any path it was renamed from. Where `evidence_for` is
line-blame-driven, `area_history` reaches commits for code that's since been deleted, moved, or
fully rewritten.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | str | *required* | The file path, as it appears at HEAD (or any commit - the rename chain is bidirectional). |
| `limit` | int | `10` | Cap on commits returned, newest first, 1 to 100. |
| `include_renames` | bool | `true` | Walk the `renamed_from` chain to include commits that touched historical names. |
| `detail` | `"compact"` \| `"full"` | `"compact"` | As for `whygraph_evidence_for`. |
| `describe` | bool | `true` | As for `whygraph_evidence_for`, with the same bound. |

Returns `{ "path", "include_renames", "evidence": [...], "describing": 0 }`, using the
[evidence item](#evidence-items) shape (`lines` is always `null`: these commits are not found by
blame).

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
| `force_refresh` | bool (default `false`) | Skip the cache and generate a new card (still stored; the same permission and budget checks apply). |
| `cached_only` | bool (default `false`) | On a cache miss return `{ "status": "not_cached", "target": {...} }` at once: no generation, no description backfill, no LLM spend. Use it to read what is already known. Has no effect with `force_refresh`, nor on a [linked](#linked-projects) project, whose card the platform serves. |

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
  "evidence_count": { "commits": 8, "prs": 3, "issues": 2 },
  "evidence": [
    {
      "sha": "...",
      "subject": "...",
      "pull_requests": [{ "number": 847, "html_url": "..." }],
      "issues": [{ "number": 12, "html_url": "..." }]
    }
  ],
  "generated": false
}
```

The card carries exactly five narrative fields - **purpose, why, constraints, tradeoffs, risks** -
plus provenance (`model`, `provider`, `cached_at`) and an `evidence_count` summary. `evidence` cites
the top five commits (newest first) with their PR and issue links, so you can name "PR #847" without
an evidence call; it is derived on each call, not stored with the card. `generated` is `true` when
this call paid for the card and `false` when it was read from the cache.

!!! note "Calls the LLM on a cache miss"
    On a miss, this hits the configured provider and may take several seconds. A hit is sub-second.
    Generation needs a credential - see [Configuration](configuration.md).

**Progress.** When the client sends a progress token, the call reports three steps: "evidence
collected (n commits)", then on a cache miss "describing k commits" (only when some commit needs a
description; the same 5-commit / 20-second bound applies) and "generating card". It also logs the
cache hit or miss as an MCP log message.

## Resources

Read-only, JSON, addressed by URI.

| URI | Name | Description |
|---|---|---|
| `whygraph://commit/{sha}` | `whygraph_commit` | A scanned commit and the pull requests that contain it (closing issues not inlined). |
| `whygraph://pr/{number}` | `whygraph_pull_request` | A pull request and the issues it closes. Includes full `commit_titles` and `comments`. |
| `whygraph://issue/{number}` | `whygraph_issue` | An issue and the pull requests that close it. |
| `whygraph://repo/overview` | `whygraph_repo_overview` | Repo-level summary: row counts, commit date range, scan freshness, LLM-description coverage, top contributors, and a `freshness` block. |

The overview's `freshness` block says whether the index is current, so a planner can read it once and
warn when the plan rests on an old scan:

```json
"freshness": { "head": "...", "last_scanned_head": "...", "commits_behind": 3, "codegraph_indexed": true }
```

`head` is the checkout's `HEAD`, `last_scanned_head` the default-branch commit of the last finished
scan, `commits_behind` how many commits `HEAD` has that the scan has not seen (`0` when equal,
`null` when unknown or never scanned) and `codegraph_indexed` whether the CodeGraph index exists. A
linked project's overview, `freshness` included, is the platform's, over its server clone.

## Prompts

Orchestration recipes that wire the tools into a workflow.

| Name | Title | Arguments | What it does |
|---|---|---|---|
| `whygraph_pre_edit_brief` | Pre-edit brief | `path` / `line_start` / `line_end` / `qualified_name` | Before you edit, gather rationale and history so the edit respects constraints and avoids known risks. |
| `whygraph_why_was_this_written` | Why was this written? | `path` / `line_start` / `line_end` / `qualified_name` | Recover the original intent behind a chunk of code from its commits, PRs, and closing issues. |
| `whygraph_triage_commit` | Triage a commit | `sha` | Summarize what one commit did and why, using its linked PR and closing issues. |

## Structured output

Each tool declares an `outputSchema` and returns the same object as `structuredContent` alongside the
indented JSON text block, with identical keys. Keys that do not apply to a result (such as `platform`
outside a linked project, or everything but `status` and `target` on a `not_cached` card) are absent
rather than `null`.

## Errors

A tool that cannot serve a request returns an error result (`isError`) whose text reads:

```text
[WhyGraph] {message} (code: {code}[, scope: {scope}])
```

The parenthetical appears only where the error carries a code, and the `scope` part only for the
budget refusal. Argument mistakes (a missing path, `limit` out of range) are errors with no code.
Match on the `[WhyGraph]` prefix, not on the start of the text, because the MCP client may add its own
prefix.

| Code | Scope | Meaning | What to do |
|---|---|---|---|
| `bad_target` | - | The target is malformed: both `qualified_name` and `path` were given, a required line is missing, or the lines are out of range. | Pass either `qualified_name` or `path` with `line_start` / `line_end`. |
| `not_indexed` | - | A `qualified_name` was given but the project's CodeGraph index cannot be read. | Use `path` and lines instead, or scan the project from the WhyGraph portal. |
| `symbol_not_found` | - | The `qualified_name` is not in the CodeGraph index. | Check the name (bare form, or `Class::method`), or use `path` and lines. |
| `blame_failed` | - | `git blame` failed on the target file. | The file may be untracked or the repository damaged; try another target. |
| `no_evidence` | - | The lines map to no scanned commit. | Scan the project from the WhyGraph portal, then retry. |
| `generation_not_permitted` | - | No card is cached and your role on the project (viewer) cannot generate one. | Ask a contributor or admin to generate the card. |
| `budget_exceeded` | `org`, `project`, or `member` | No card is cached and the LLM budget is reached. Cached cards still read. | Wait for the budget to reset, or ask an admin to raise it. The scope says whose budget it is. |
| `provider_error` | - | The LLM provider failed while writing the card. | Retry later; check the provider key in the portal if it keeps failing. |

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
null `llm_description`, no pull requests or issues, and in `full` an empty `text.body`) with one of:

| `push_status` | Meaning |
|---|---|
| `uncommitted` | The lines are not committed yet |
| `not_pushed` | The commit is in this checkout only: no `origin/*` ref holds it |
| `pending_scan` | Pushed to the default branch, but the platform has not scanned it yet |
| `not_on_default_branch` | Pushed, but not to the branch the platform scans |

The labels come from your `origin/*` refs as the last fetch left them; WhyGraph never fetches.
`whygraph://commit/{sha}` for a commit the platform lacks returns its git message with the same
label.

**`platform.answered`.** `whygraph_evidence_for` adds `"answered": true` to its `platform` block when
the platform accounted for the pushed commits, and `false` when it did not (nothing was pushed to ask
about, or the call failed). With `false`, read `pending_scan` and `not_on_default_branch` as local
guesses.

**Offline behaviour.** If the platform cannot be reached, is revoked or is gone,
`whygraph_evidence_for` still answers with your blame, every pushed commit labelled `pending_scan`
or `not_on_default_branch`, and a `platform.status` that says why. `whygraph_area_history`,
`whygraph_rationale_brief` and the resources need the platform's history and fail with a message that
names the fix.

**Other differences.** Only pushed commits, paths and symbol names are sent to the platform.
`whygraph_rationale_brief` returns a card generated and cached on the platform (shared by the whole
team), and fails with "no pushed history" while the target's lines are not on a pushed commit.
`whygraph_area_history` answers for a path only when a pushed default-branch revision tracks it.
`whygraph_evidence_for` never writes a description locally, so `describing` is always `0`.

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
