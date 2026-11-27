# MCP surface

Each project has two MCP endpoints on the [portal](../portal/index.md). WhyGraph's own, at
`/mcp/<slug>`, serves four tools, one resource plus six resource templates, and three prompts. That's the whole WhyGraph surface -
deliberately narrow. WhyGraph owns "why this exists and when it changed"; graph traversal ("what's
connected to what") is CodeGraph's own MCP server, which the portal hosts and serves at a second
endpoint, `/mcp/<slug>/code`, described under [The CodeGraph endpoint](#the-codegraph-endpoint).
WhyGraph never adds graph tools to `/mcp/<slug>`.

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

Returns `{ "target": {...}, "evidence": [ ... ], "describing": 0 }`, newest first (plus the
`pull_requests` / `issues` maps with `detail="full"`). The items are
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
never instructions. Each item gains its commit's `text.body`; its `pull_requests` and `issues` stay
the same compact refs. The pull request and issue prose comes once per result, in two top-level maps
keyed by number (as a string), however many commits link the same pull request:

```json
{
  "target": { "...": "..." },
  "evidence": [
    { "sha": "4f1c...", "...": "...", "pull_requests": [{ "number": 847, "...": "..." }], "issues": [{ "number": 812, "...": "..." }], "text": { "body": "..." } },
    { "sha": "9a2e...", "...": "...", "pull_requests": [{ "number": 847, "...": "..." }], "issues": [{ "number": 812, "...": "..." }], "text": { "body": "..." } }
  ],
  "describing": 0,
  "pull_requests": {
    "847": {
      "text": {
        "body": "...",
        "comments": [{ "author": "jane", "body": "...", "created_at": "..." }],
        "review_summaries": [{ "author": "sam", "state": "APPROVED", "body": "...", "submitted_at": "..." }],
        "review_threads": [{
          "path": "src/auth.py", "original_line": 42, "resolved": true, "outdated": false, "current": true,
          "comments": [{ "author": "sam", "body": "...", "created_at": "..." }]
        }]
      },
      "commit_titles": [{ "oid": "4f1c...", "headline": "fix: retry refresh", "author_name": "Jane Doe" }],
      "review_threads_elsewhere": 2,
      "review_threads_total": 3,
      "omitted": { "comments": 3, "commit_titles": 0, "body_chars": 0 }
    }
  },
  "issues": {
    "812": { "text": { "body": "...", "comments": [{ "author": "ana", "body": "...", "created_at": "..." }] } }
  }
}
```

A pull request entry holds `text.body`, `text.comments` (the conversation), `text.review_summaries`
(the review bodies and their state), `text.review_threads` and `commit_titles` (the first
`pr_roster_max_commits`); an issue entry holds `text.body` and `text.comments`. Comments and
review summaries are the oldest first, at most `[rationale].pr_discussion_max_comments` of each, and
every comment is cut to `pr_comment_max_chars`. Pull request and issue bodies are cut at **4000**
characters (marked with `...`); commit bodies are not cut. Bot and hidden (minimized) comments are
not stored, so they never appear, except a bot's or hidden opening comment of a thread a human
answered (cut short).

`review_threads` holds only the threads on **the target's file** (renames followed), at most
`pr_discussion_max_comments` comments in all. Each thread carries `current`: `true` when the code it
was written about still exists in today's file, `false` when it is gone or cannot be placed. The
threads on other files are not inlined: `review_threads_elsewhere` counts them and
`whygraph://pr/{n}` carries every file's. `review_threads_total` is GitHub's own count of threads on
the pull request, which can exceed what was stored (the first 20 threads are kept).

An entry that lost anything carries `omitted`: on a pull request `{ "comments": n, "commit_titles": m,
"body_chars": k, "review_summaries": a, "review_comments": b, "review_threads_elsewhere": c }`, on an
issue `{ "body_chars": k, "comments": n }`.

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

Returns `{ "path", "include_renames", "evidence": [...], "describing": 0 }` (plus the
`pull_requests` / `issues` maps with `detail="full"`), using the
[evidence item](#evidence-items) shape (`lines` is always `null`: these commits are not found by
blame). When committed [plans](#plans) name the file it also returns a `plans` list, the
file-level form described under [Plans](#plans).

### `whygraph_propose_note`

Propose a short [project note](../guide/notes.md) - a decision, constraint, gotcha or convention the
code does not show. This is the one tool that writes, and it only **proposes**: the note waits for a
member to confirm it in the portal, and no model reads it before then. There is deliberately no tool
that lists or searches notes; confirmed notes arrive with the rationale card and through
`whygraph://note/{id}`.

| Parameter | Type | Description |
|---|---|---|
| `body` | str | The note, at most 1,000 characters, no control characters other than newlines. |
| `kind` | `"decision"` \| `"constraint"` \| `"gotcha"` \| `"convention"` | What it records. |
| `qualified_name` | str | Anchor: a symbol. A name defined in several files is refused; anchor the file or lines instead. |
| `path` | str | Anchor: a file or folder. With `line_start` and `line_end`, a line range of that file. |
| `line_start`, `line_end` | int | The lines of a range anchor (1-indexed, inclusive). |
| `area` | str | Anchor: a configured [area](configuration.md#areas). |
| `project_wide` | bool | Anchor: the whole project. |
| `refs` | list of str | Up to five commit SHAs (7 or more characters) or `#N` pull requests and issues, each present in the project's history. |

Give **exactly one** anchor: `qualified_name`, `path` (with optional lines), `area` or
`project_wide`. Returns `{ "note_id", "status", "duplicate", "review", "note" }`: `review` is the
portal page where a person reviews it (`/p/<slug>/notes?id=<id>`), and `duplicate` is `true` when the
same text on the same anchor already existed (the existing note is returned and nothing new is
created). When the match is another member's pending proposal, `note` is left out. A project-wide
note, or one on a top-level folder, needs a second person to confirm it.

The proposer is the local user. Refusals: `note_invalid` (the text, kind, anchor or refs failed
validation), `note_limit` (20 agent proposals per member and 50 per project per UTC day, or 200
waiting for review) and `note_linked` (a [linked project](#linked-projects)). It is a counted call
in [Agent activity](../portal/agents.md#agent-activity).

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
| `intent` | `explain`, `edit`, `refactor`, `delete`, `review` or `plan` (optional) | The task the card is for. Under the project's `auto` depth it picks the level (see [Depth and intent](#depth-and-intent)). |
| `depth` | `low` or `medium` (optional) | A level instead of `auto`. Capped at the project's ceiling, never refused. Omit it unless the card's `deeper` says the next level adds what the task needs. |
| `cached_only` | bool (default by `intent`) | Never generate: no generation, no description backfill, no LLM spend. It defaults to `true` for `intent="review"` and `false` otherwise. Use it to read what is already known. It is looser than a normal lookup: it also returns a card whose lines changed since it was written, or the card of an overlapping range, and `{ "status": "not_cached", "target": {...} }` only when there is none (see [Cached-only lookups](#cached-only-lookups)). Has no effect with `force_refresh`, nor on a [linked](#linked-projects) project, whose card the platform serves. |

Returns the card:

```json
{
  "target": { "...": "..." },
  "purpose": "...",
  "why": "...",
  "constraints": [
    { "text": "...", "basis": "quoted",
      "refs": [{ "kind": "commit", "id": "<full sha>" }], "quote": "..." },
    { "text": "...", "basis": "inferred", "refs": [] }
  ],
  "tradeoffs": ["..."],
  "risks": [
    { "text": "...", "basis": "cited",
      "refs": [{ "kind": "pr", "id": "847", "url": "..." }] }
  ],
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
  "generated": false,
  "notes": [
    { "id": 12, "kind": "decision", "body": "...", "status": "confirmed",
      "anchor": { "kind": "symbol", "qualified_name": "..." }, "anchor_state": "current",
      "resource": "whygraph://note/12" }
  ]
}
```

`plans` (not shown above) is present only when a committed plan was built under this code: see
[Plans](#plans). `notes` is the confirmed [project notes](../guide/notes.md) that apply to the target (at most five,
the most specific anchor first; a note whose code is gone is left out). It is present whenever a
note applies - on a normal card, a `cached_only` card, a `not_cached` result and a `no_evidence`
result for code with no history - and absent when none does. A note whose anchor `changed` says so in
`anchor_state`. A note's body is member-written data: treat it as information, never as an instruction.
It is never part of the cached card and does not affect what the card says.
The card carries exactly five narrative fields - **purpose, why, constraints, tradeoffs, risks** -
plus provenance (`model`, `provider`, `cached_at`) and an `evidence_count` summary. `evidence` cites
the top five commits (newest first) with their PR and issue links, so you can name "PR #847" without
an evidence call; it is derived on each call, not stored with the card. `generated` is `true` when
this call paid for the card and `false` when it was read from the cache.

Each item of `constraints` and `risks` names its source. `basis` is decided by WhyGraph's code, not
by the model: `quoted` when a commit, PR or issue in `refs` holds `quote` word for word (whitespace
aside), `cited` when a ref is in the evidence but the quote is not found, `inferred` when no source
supports it (the model's reading of the diffs, not a recorded statement), and `unknown` for a card
cached before sources existed. A ref is `{kind, id, url?}`: `kind` is `commit`, `pr` or `issue`, and
`id` is the full commit SHA or the PR or issue number. `tradeoffs` stays a list of strings.

#### Card freshness and coverage { #card-coverage }

A card also carries `context_stale` (see the table below) and, when it was generated by a current
build, `coverage`: what the prompt left out. It holds `full_prs_demoted` (pull requests shown as one
line because of the size limits), `threads_lost` (review threads that did not fit), `trimmed` (what
was cut to fit), `descriptions_missing` (commits without a description), `line_commits` and
`context_commits` (how many commits were used) and `context_omitted` (file-history commits that did
not fit). A card cached before this version has no `coverage`.

#### Depth and intent { #depth-and-intent }

A card is built at one of two levels (see [Concepts](../guide/concepts.md#depth-levels)); the card's
`depth` says which: `{requested, resolved, reason}`, where `requested` is what was asked (`auto` or a
level), `resolved` is the level used and `reason` is a short sentence saying why. Under `auto`:

| `intent` | Level |
|---|---|
| `explain`, `review` | `low`. `review` also accepts a cached card of any depth. |
| `plan`, `edit`, `refactor`, `delete`, none | `medium` |

One level up when the symbol has five or more distinct callers (matched by name), one level down when
a budget that covers the caller is at 75 % or more of its limit, and never above `medium` or the
ceiling. The `reason` names only what moved the level. An
explicit `depth` skips all of that except the ceiling. A cached card of the resolved level is served;
a shallower one is regenerated at the resolved level, a deeper one is served as it is.

`deeper`, when present, is `{level, adds}`: what the next level would add for this target, so only a
`low` card can carry it (`null` at `medium` or at the ceiling). Ask for it at most once, with `depth`,
and only when the task needs what it adds. `coverage` gains `source` (`included`, `omitted` for a
target over 8,000 characters, or `unavailable`). A card cached before depth levels has none of these
fields.

#### Cached-only lookups { #cached-only-lookups }

A `cached_only` call is meant for reading what is already known, such as a review of a branch whose
symbols have moved or gained commits, so it accepts cards a generating call would replace. In order:

1. The card cached for exactly this range, with unchanged evidence.
2. The card cached for exactly this range whose lines got new commits since it was written: it is
   returned with `stale: true` instead of being regenerated.
3. Otherwise, the best card of the same file (same provider and model) whose range overlaps the
   requested one: first a card for the same `qualified_name`, then the smallest range that contains
   the request, then the longest overlap, the newest on a tie. `stale` compares the card's own range
   with its current evidence.
4. Otherwise `{ "status": "not_cached", "target": {...} }`.

Every card a `cached_only` call returns carries three more keys, and an overlap a fourth:

| Field | Type | Meaning |
|---|---|---|
| `match` | `"exact"` or `"overlap"` | Whether the card was cached for the requested range or for an overlapping one. |
| `stale` | bool | `true` when the card's lines got new commits since it was written: it is the rationale from before that change. |
| `context_stale` | bool | `true` when one or two newer commits touched the file but not the card's lines. The card is still valid for the lines; those commits are not in it. Three or more newer file commits make a miss instead. Apart from `stale`, which means the lines changed. |
| `card_range` | `{ "line_start", "line_end" }` | With `match: "overlap"` only: the range the card was written for. `target` stays the requested range. |

`generated` is always `false`. For an overlap card, `evidence_count` and `evidence` describe the
card's own range. A call without `cached_only` never returns these keys, and treats a stale or
overlapping card as a miss.

!!! note "Calls the LLM on a cache miss"
    A hit is sub-second, a partial card included. On a miss, the call first describes the commits that
    have no description yet (up to `[analyze].max_workers` of them in parallel, within 20 seconds), then
    generates the card. A card built while some commits were still undescribed comes back with
    `partial: true`, `describing` (how many commits were still waiting) and `completing` (whether the
    rest is being described in the background). The completion runs at most once per card, with a
    10-minute cooldown, and is charged as a second card generation. `whygraph_evidence_for` and
    `whygraph_area_history` keep their 5-commit / 20-second bound and start no background work; their
    `describing` counts every commit still waiting. Generation needs a credential - see
    [Configuration](configuration.md).

**Progress.** When the client sends a progress token, the call reports three steps: "evidence
collected (n commits)", then on a cache miss "describing k commits" (only when some commit needs a
description; the call describes up to `[analyze].max_workers` commits in parallel within 20 seconds, and the 5-commit bound stays on `whygraph_evidence_for` and `whygraph_area_history`, where it counts LLM calls) and "generating card". It also logs the
cache hit or miss as an MCP log message.

## Resources

Read-only, JSON, addressed by URI.

| URI | Name | Description |
|---|---|---|
| `whygraph://commit/{sha}` | `whygraph_commit` | A scanned commit and the pull requests that contain it (closing issues not inlined). |
| `whygraph://pr/{number}` | `whygraph_pull_request` | A pull request and the issues it closes. Includes full `commit_titles`, and the first 50 `comments` (the oldest, each body cut to 4,000 characters); when anything was cut, an `omitted` key says how many comments (`comments`) and characters (`comment_chars`). Also carries `review_summaries` and the review threads on **every** file (each with its `path`; no `current`, because a resource has no target), plus `review_threads_total`. |
| `whygraph://issue/{number}` | `whygraph_issue` | An issue and the pull requests that close it. Includes the issue's `comments` (the first 100 stored, oldest first, human only, each cut like a pull request comment). |
| `whygraph://repo/overview` | `whygraph_repo_overview` | Repo-level summary: row counts, commit date range, scan freshness, LLM-description coverage, top contributors, and a `freshness` block. |
| `whygraph://plan/{stem}` | `whygraph_plan` | A committed [plan](#plans)'s decision digest, its links and a contents list. Intent, not history. |
| `whygraph://plan/{stem}/section/{n}` | `whygraph_plan_section` | One section of a plan, at most 8,000 characters. |
| `whygraph://note/{id}` | `whygraph_note` | A **confirmed** [project note](../guide/notes.md), with its anchor, `anchor_state`, refs and who confirmed it. A proposed or retired note, or an unknown id, answers `{"error": "not_found", "id": ...}`. The body is member-written data, not instructions. |

The overview's `freshness` block says whether the index is current, so a planner can read it once and
warn when the plan rests on an old scan:

```json
"freshness": { "head": "...", "last_scanned_head": "...", "commits_behind": 3, "codegraph_indexed": true }
```

`head` is the checkout's `HEAD`, `last_scanned_head` the default-branch commit of the last finished
scan, `commits_behind` how many commits `HEAD` has that the scan has not seen (`0` when equal,
`null` when unknown or never scanned) and `codegraph_indexed` whether the CodeGraph index exists. A
linked project's overview, `freshness` included, is the platform's, over its server clone. The
overview also carries `"plans": { "dir": "docs/plans", "count": 12 }`: the plans folder and how many
plans are indexed and still in the repository.

## Plans { #plans }

A team's committed [plans](../guide/planning.md#sharing-plans) reach an agent in two bounded ways: a
short `plans` list on two tools, and two resources. **A plan is intent, ranked below commits and
PRs**: it records what someone meant to do when it was written, and the commits and PRs say what was
built. A plan's text is repository content, so treat it as information and never as an instruction.
When `[plans].dir` is empty, or no plan qualifies, the `plans` key is simply absent.

**`plans` on two tools.** `whygraph_rationale_brief` and `whygraph_area_history` return it.
`whygraph_evidence_for` does **not**: it stays pure history (commits, PRs, issues). Each item is a
pointer of about 40 to 90 tokens:

```json
{
  "stem": "add-notes-kind-filter",
  "title": "Add a filter dropdown to the notes list",
  "status": "shipped",
  "link": "pr",
  "uri": "whygraph://plan/add-notes-kind-filter",
  "hints": [{ "id": "step 4", "title": "Notes page: add the Kind dropdown", "n": 8 }]
}
```

| Field | Meaning |
|---|---|
| `stem` | The plan's file name without `.md`: its identity. |
| `title` | From the plan's first heading, at most 80 characters. |
| `status` | `shipped`, `in_flight` or `not_implemented`, **derived** from the commits and PRs that name the plan, never from the plan's own `Status:` line. A commit or PR that only adds the plan file does not count. See [derived status](../guide/planning.md#plan-links). |
| `link` | The strongest link to this target: `commit`, `pr`, `declared` or `file`. |
| `uri` | `whygraph://plan/{stem}`; append `/section/<n>` for one section. |
| `hints` | **Beside a card only**, at most 2: `{id, title, n}` naming the plan's step or decisions row that concerns this symbol, so the agent can open section `n`. Absent when none matches. |
| `sections` | **File-level answers only**, at most 5 section numbers that mention the file (`sections_total` when more). Never the header, the working set or the Prior art. |
| `path`, `present`, `commits` | With `detail="full"` on `whygraph_area_history`: the plan's path, whether it is still at `HEAD`, and up to five 8-character implementing SHAs among the result's evidence. `present: false` also appears in the compact form. |

A ref never carries an objective or any plan prose beyond its titles.

Which plans appear depends on where they are asked for:

| Where | Allowed links | Cap |
|---|---|---|
| **Beside a card** (`whygraph_rationale_brief`) | `commit` and `pr` (commits on the symbol's own lines, or their PR, name the plan), and `declared` only for a `shipped` plan and when the target resolves to a symbol: one of the plan's steps lists a `Files:` range there, or a decisions row names it, and a change to the file backs it | 3 refs, 2 hints each, 1,200 characters in all |
| **File-level** (`whygraph_area_history`) | `commit`, `pr`, `declared` and `file`, any status; a plan that is not yet built ranks last, and a `file` link (a plan that only names the file) is limited to 3 of the 5 | 5 refs |

A name in a plan's prose never links it to a symbol beside a card: a plan that says "no change needed
in `foo`" is not about `foo`. Pending intent therefore shows only in file-level answers, not next to
the reasons for existing code. Text that only copies WhyGraph's own output, such as quoted card
constraints, never makes a link.

**The resources.** There is no way to fetch a whole plan through MCP, however long it is.

| URI | Name | Description |
|---|---|---|
| `whygraph://plan/{stem}` | `whygraph_plan` | The plan's decision digest, its links and a contents list of its top-level sections. Start here. |
| `whygraph://plan/{stem}/section/{n}` | `whygraph_plan_section` | One section of the plan, at most 8,000 characters. Take `n` from a ref's `hints` or `sections`, or from the contents. |

Both answer with `"kind": "plan"` and a fixed `"reading"` line, `"Intent when it was written. Commits and
PRs say what was built."`, so the framing travels with the data, plus the derived `status`.

- **The digest** (`whygraph://plan/{stem}`) holds `title`, `objective`, `path`, `present`, the
  `digest` itself (at most 3,000 characters: the plan's objective and non-goals, its resolved
  decisions, any changes made on purpose, open questions with defaults, and the plan's own stated
  risks), `implementing_commits` and `pull_requests` (at most 5 each, with `..._total`), up to 10
  `paths` (with `paths_total`), `publishing_links`, `redacted_lines` and `contents`: the top-level
  sections as `{n, title, chars, children}`, at most 40 (`contents_total` when more). The whole answer
  stays under 8,500 characters.
- **A section** holds `n`, `title`, `level`, `text` (the section's own text, at most 8,000 characters:
  a longer section is split into parts titled `... (continued 2/3)`), `chars`, `children`, `prev` and
  `next`, and `version` (the first 8 characters of the plan file's blob SHA, so a stale number is
  visible after the plan is edited). When the section holds quoted copies of rationale cards or the
  Prior art, it also carries `copies: true` and a note saying to read the current card or
  `whygraph_area_history` instead. The first section (`n = 0`, the header) is served with the plan's
  own, never-updated `Status:` line replaced by a pointer to the derived `status`.

An unknown stem or section number answers `{"error": "not_found", ...}`.

**Delivery is three bounded tiers**: a ref (about 50 tokens), the digest and contents (about 1,000
tokens), one section (at most about 2,000 tokens). A person reading the Plans page sees the whole plan,
which costs no tokens.

!!! note "On a linked project"
    A [linked project](#linked-projects) gets plans from the platform: only plans merged to the
    repository's default branch. See [Projects from a platform](../portal/platform-projects.md#plans).

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
| `note_invalid` | - | `whygraph_propose_note`: the text, kind, anchor or refs failed validation (several anchors, a path outside the repository, a symbol in several files, a ref that is not in the project's history). | Fix the argument the message names. |
| `note_limit` | - | `whygraph_propose_note`: 20 agent proposals per member or 50 per project today (UTC), or 200 waiting for review. | Ask a member to review the waiting proposals, or try again tomorrow. |
| `note_linked` | - | `whygraph_propose_note` on a [linked project](#linked-projects). | Propose the note on the platform. |

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

**No notes.** A linked project has no local notes database: `whygraph_propose_note` fails with
`note_linked`, `whygraph://note/{id}` answers `not_found`, and the platform's cards carry no `notes`.

**Plans.** `whygraph_area_history` and `whygraph_rationale_brief` add the platform's [plans](#plans) to
their results, and `whygraph://plan/{stem}` and `.../section/{n}` read the platform's copy. Only the
SHAs and the path the call already sent cross the link, plus the stem a plan read names; plans are
never computed from your uncommitted or unpushed work. The platform serves only plans merged to its
default branch. A platform too old to know plans leaves `plans` out and sets `"plans": "unsupported"`
in the result's `platform` block; the link stays `ok`.

**Other differences.** Only pushed commits, paths and symbol names are sent to the platform.
`whygraph_rationale_brief` returns a card generated and cached on the platform (shared by the whole
team), and fails with "no pushed history" while the target's lines are not on a pushed commit.
`whygraph_area_history` answers for a path only when a pushed default-branch revision tracks it.
`whygraph_evidence_for` never writes a description locally, so `describing` is always `0`.

## The CodeGraph endpoint

`http://127.0.0.1:<port>/mcp/<slug>/code` is CodeGraph's own MCP server, run by the portal as one shared
child process (`codegraph serve --mcp`) and bridged per project. Agents see it as a second server named
`codegraph`, written next to `whygraph` when you [set up a project](../portal/agents.md#what-gets-written).
It is stateless Streamable HTTP like the first endpoint, with the same gate: local mode only (a
production portal answers `404`), the same `Host` and `Origin` checks, the project must be
initialized (otherwise `409`), and a request body over 1 MiB is refused with `413`.

**Tools.** Four read-only tools are served. All four are annotated `readOnlyHint`, `idempotentHint`,
not destructive and not open-world, and the descriptions are CodeGraph's own.

| Tool | Arguments | Use |
|---|---|---|
| `codegraph_explore` | `query` (required), `maxFiles` (1-20) | How a feature works: the line-numbered source of the relevant symbols grouped by file, the call paths among them and a blast-radius summary, in one call |
| `codegraph_search` | `query` (required), `kind`, `limit` (1-100) | Where a symbol is defined: locations only, no source |
| `codegraph_node` | `symbol`, `file`, `line`, `includeCode`, `symbolsOnly`, `offset`, `limit` (1-2000 lines) | One symbol's signature, source and **Called by** / **Calls** trail, or a whole file with line numbers (pass `file` alone) |
| `codegraph_impact` | `symbol` (required), `file`, `depth` (1-3) | What changing a symbol affects, transitively |

`codegraph_callers` and `codegraph_callees` are not served; the call fails with `-32602` and a message
naming the replacement (read the **Called by** / **Calls** trail of `codegraph_node`, or use
`codegraph_impact`). Any other tool name fails the same way and is never counted.

**Arguments are policed.** The advertised schemas reject unknown properties. `projectPath` is removed
from them: the portal always pins it to the project's root, and a value that names anything else is
refused with "this endpoint serves only <slug>". `file` (on `codegraph_node` and `codegraph_impact`)
must stay inside the project, `depth` is at most 3, and strings and numbers have size caps. A refused
call is never forwarded.

**Results and errors.** A forwarded result passes through unedited, with a note appended when it is
cut at 256 KiB. Every message the portal itself writes begins `[WhyGraph]`; match on that prefix and on
`isError`, never on text inside a successful result, which is repository content.

| Situation | Result | Text begins |
|---|---|---|
| Argument refused | error | `[WhyGraph] <argument> ...` |
| No index yet | **success** (not an error) | `[WhyGraph] This project has no code index yet. Run a scan ...` |
| Index still refreshing after 45 s | error | `[WhyGraph] The code index is still updating: ...` (names the seconds to wait) |
| Refresh failed | error | `[WhyGraph] The code index could not be refreshed. ...` |
| A symbolic link leaves the repository | error | `[WhyGraph] Indexing is refused: ...` |
| More calls than the server can take | error | `[WhyGraph] CodeGraph is busy. ...` |
| A call exceeded 60 s | error | `[WhyGraph] The CodeGraph call took longer than 60 s ...` |
| The shared server crashed and restarted | error | `[WhyGraph] CodeGraph restarted; call again.` or `... is paused on this portal ...` |
| `[scan].serve_codegraph` is false | error | `[WhyGraph] The CodeGraph tools are turned off for this project ...` |
| No `codegraph` command on the portal | error | `[WhyGraph] CodeGraph is not available on this portal ...` |

**Freshness and progress.** Each call first makes sure the index has caught up with the working tree,
waiting up to 45 seconds; see [Freshness](../guide/mcp-usage.md#freshness). While it waits, a client
that sent a progress token receives a progress notification every 3 seconds ("Refreshing the code index
(3 changed files, 6s)", or a message that it is waiting for another project's refresh or for a scan).

**Instructions and schema pin.** The server's instructions are WhyGraph's (short, and they tell the
agent how to read the errors above); CodeGraph's own are never sent. The portal pins the tool schemas it
was built against. If the installed CodeGraph lists a tool whose arguments or annotations differ, that
tool is withheld and the project's Overview says so.

The server stops after 10 idle minutes and starts again on the next call. Only the four tool names and
counts are recorded; see [Agent activity](../portal/agents.md#agent-activity) and the
[security model](../portal/security.md#the-codegraph-endpoint).

## Composition with CodeGraph

**The `whygraph` server** exposes no graph-traversal tools on purpose. The split:

| Layer | Owns |
|---|---|
| **CodeGraph** | "what is connected to what" - callers, callees, symbol resolution, type hierarchy. |
| **WhyGraph** | "why does this exist and when did it change" - evidence, rationale, history. |

For traversal mid-conversation, call the `codegraph` server's tools: the portal serves them for you
(see above), so there is nothing to install alongside WhyGraph's.

This is a boundary on the MCP surface, not on WhyGraph's access to the graph. The
[Explorer](../guide/playground.md) and the [chat assistant](../guide/chat.md) both traverse it,
because they read CodeGraph's database in-process rather than over MCP.
