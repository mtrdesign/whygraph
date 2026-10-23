# Unreleased

!!! note "Not released yet"
    This page collects what is going into the next release. It moves into a numbered release page when
    that release is cut.

The portal now runs **CodeGraph's own MCP server** for you, so an agent gets graph tools without
installing anything, and WhyGraph's own MCP tools, agent assets and planning pipeline get a round of
fixes and additions.

## What's new

- **CodeGraph's tools, served by the portal.** Every project has a second endpoint,
  `/mcp/<slug>/code`, and initializing a project writes a second agent entry named `codegraph` beside
  `whygraph`. It serves four read-only tools (`codegraph_explore`, `codegraph_node`, `codegraph_search`,
  `codegraph_impact`) from one shared server that starts on demand and stops after 10 idle minutes. Each
  call first waits (up to 45 seconds) for the index to catch up with your uncommitted edits; past that it
  fails with a retryable message. See [The CodeGraph endpoint](../reference/mcp.md#the-codegraph-endpoint)
  and [Freshness](../guide/mcp-usage.md#freshness). Turn it off per project with
  `[scan].serve_codegraph = false` (Settings > Scanning); it is also honoured by linked projects.
  Codex entries carry `tool_timeout_sec = 120`.
- **The Overview shows the CodeGraph server.** New health items cover a paused server, a refused symbolic
  link, a failed refresh, withheld tools, the switch being off and a missing binary, with **Open
  CodeGraph log** and **Restart CodeGraph**, plus a server line on the Agent activity card.
- **`/whygraph-review` (Claude Code).** Checks a diff against the cached rationale cards and reports, per
  hunk, a constraint at risk, a risk triggered, no rationale or consistent. It spends no LLM budget unless
  you pass `--generate`. See [Reviewing a diff](../portal/agents.md#whygraph-review).
- **WhyGraph MCP changes.**
    - `whygraph_evidence_for` and `whygraph_area_history` now return **compact items by default** (no
      commit bodies or comments); pass `detail="full"` for the prose. The default `limit` is 10, and a
      call describes at most 5 missing commit descriptions within 20 seconds (`describe=false` skips
      the LLM).
    - `whygraph_rationale_brief` gains `force_refresh` and `cached_only` (a miss returns `not_cached`
      at no cost), reports progress, and cites the top five commits with their PR and issue links.
      **`cached_only` is not honoured by linked projects**: their cards come from the platform.
    - A `cached_only` rationale call is looser than a generating one: it returns a card whose lines got
      new commits since (`stale: true`) or the card of an overlapping range of the same file
      (`match: "overlap"`, `card_range`), so `/whygraph-review` finds the card of a symbol that moved or
      changed instead of reporting no rationale. See
      [Cached-only lookups](../reference/mcp.md#cached-only-lookups).
    - Errors read `[WhyGraph] ... (code: ..., scope: ...)` with a documented
      [code table](../reference/mcp.md#errors), every tool declares an output schema, and tools carry
      annotations and titles.
    - `GET /api/v1/overview` and the `whygraph://repo/overview` resource gain a `freshness` block
      (`head`, `last_scanned_head`, `commits_behind`, `codegraph_indexed`).
- **Planning pipeline.** The planner inlines trimmed rationale cards and writes the plan file itself,
  a failed researcher no longer stops the run, `verification` replaces the `impact` research lens, and
  `/whygraph-implement` takes `--fix-attempts N`.
- **Cursor commands are now skills.** `/rationale`, `/whygraph-plan` and `/whygraph-implement` install
  under `.cursor/skills/`. A project initialized earlier keeps its old `.cursor/commands/` files; delete
  them by hand if you do not want duplicates.

## Upgrade path

1. Re-run the installer and `whygraph up` as usual.
2. **Open each project's Settings > Agents and use Update agent files, once per project.** It adds the
   `codegraph` entry and refreshes the bundled assets. The portal now records a hash of every file it
   installed in `.whygraph/assets.json`, so files you have not edited update silently and edited ones are
   listed with a diff; a project initialized before the manifest existed asks about each file once. Tick
   **Overwrite my edits too** to replace edited files as well.
3. In Claude Code, approve the new `codegraph` server when asked.
4. If you installed CodeGraph on your host, remove it with `codegraph uninstall --keep-cli`. See
   [If you installed CodeGraph yourself](../portal/agents.md#own-codegraph).
5. **The first scan of each project rebuilds its code index** once, in full, because CodeGraph indexed
   files reached through a symbolic link that leaves the repository on purpose. A project that has such a
   link now refuses to index until you remove it (the Overview names the links); see
   [Symbolic links](../portal/security.md#symbolic-links). A production clone is now made without
   symbolic links.

## Footprint

The shared CodeGraph server uses about 130-300 MiB of RAM while it runs and is stopped after 10 idle
minutes. The portal also keeps `locks/codegraph/`, `logs/codegraph.log` and `codegraph-home/` under its
data directory; all three are disposable.
