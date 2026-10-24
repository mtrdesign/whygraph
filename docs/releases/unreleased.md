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
    - With `detail="full"`, pull request and issue prose comes **once per result**, in top-level
      `pull_requests` / `issues` maps keyed by number, instead of on every commit of the same pull
      request; the items keep the compact refs. Pull request and issue bodies are cut at 4000
      characters (counted in `omitted.body_chars`).
    - `whygraph_rationale_brief`'s `evidence_count` counts each pull request and issue once, not
      once per commit that links it.
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
  under `.cursor/skills/`. A project initialized earlier has old `.cursor/commands/` files: **Update
  agent files** removes them (it asks first if you edited one).
- **Update agent files removes what WhyGraph no longer ships.** A bundled file that a newer version
  dropped or renamed is removed when you have not edited it, and listed as **Edited** (removed only with
  **Overwrite my edits too**) when you have. See
  [Keeping the agent files up to date](../portal/agents.md#agent-files-upgrade).

## Security fixes

- **A scan no longer imports Python code from the repository it scans.** The portal's scan child runs
  inside the project's folder, and Python looked there first for its own modules, so a repository that
  committed a `whygraph/` package could run code in the portal. The child now starts with Python's safe
  path mode (`-P`, `PYTHONSAFEPATH=1`), which leaves the working folder off the module search path. See
  [Repository content is not trusted](../portal/security.md#repository-content-is-not-trusted).

## Breaking changes

This release removes `whygraph scan`, and moves each project's WhyGraph history out of the repository into the portal's data directory.

- **`whygraph scan` is removed.** It is now a stub like `whygraph init` and `whygraph serve`: it prints a
  pointer to the portal on stderr and exits with status `2`. The portal runs every scan itself, as an
  internal child process that nobody types; use **Scan now** (Quick rescan or Full rescan) or the git
  hooks. A CI job that ran `whygraph scan` has no replacement.
- **Docker is the only install.** The `uv tool install`, `pipx` and `git+https` routes (which only ever
  gave the headless scan) are gone. The PyPI name `whygraph` belongs to an unrelated project.
- **Credentials from your environment no longer apply anywhere.** The one-shot shim no longer passes
  `GH_TOKEN`, `GITHUB_TOKEN` or the provider API keys into its container, and ambient `GH_TOKEN` /
  `GITHUB_TOKEN` are not read by any scan. A `[scan].token` in a `whygraph.toml` is imported **once**,
  into the project's encrypted GitHub token, and is never read at scan time.
- **`whygraph.toml` is only an import.** The portal reads it once when you add a project; nothing else
  reads it. The `whygraph_db` and `codegraph_db` overrides, the top-level `log_level` and the `[logging]`
  table are gone.
- **`.whygraph/scan.log` is gone.** A run's full log is on its page under **Scans**.
- **The production run log's first line changed.** It reads `$ scan <flags>` (for example
  `$ scan --skip-analyze --no-remote`) instead of `$ whygraph scan <flags>`. Local mode logs the real
  command line of the scan child.
- **A project's history moves to the data directory.** Commit descriptions, rationale cards and chats
  (`whygraph.db`) are now in `<data>/projects/<org>/<slug>/` (`~/.local/share/whygraph/projects/local/<slug>/`
  locally, `/data/projects/<org>/<slug>/` in production), in both modes, so `git clean -xdf`, a re-clone
  or a moved checkout no longer loses them. `.codegraph/` stays in the repository. Migration backups go
  to `projects/<org>/<slug>/backups/`.
- **An in-repo `.whygraph/whygraph.db` is never read.** A database from 2.0.0 or 1.x left in a
  repository is ignored, so a project added after this release starts with empty history.
- **Removing a project differs by mode.** Local mode keeps the history unless you tick **Also delete
  history** (typed name required); production deletes it unless an admin ticks **Keep history**. Adding
  the same repository again re-attaches kept history (matched by its `origin` remote, by path when there
  is none, or by GitHub repository id in production); an unrelated repository with the same name gets
  `<name>-2`. A production re-import does not restore access restrictions. A linked project has no
  history and never deletes any; deleting an organization removes `projects/<org>/`.
- **Back up the whole data directory.** `whygraph backup` is still `pg_dump` only, so it no longer covers
  everything: the data directory holds both `postgres/` and `projects/`.
- **`make db`** opens the project's data-directory database; re-copy `docker-compose.example.yml` to
  `docker-compose.yml` to get it.

## Upgrade path

1. Re-run the installer and `whygraph up` as usual.
2. **Open each project's Settings > Agents and use Update agent files, once per project.** It adds the
   `codegraph` entry and refreshes the bundled assets. The portal now records a hash of every file it
   installed in `.whygraph/assets.json`, so files you have not edited update silently (or are removed,
   such as Cursor's old `.cursor/commands/`) and edited ones are listed with a diff; a project
   initialized before the manifest existed asks about each file once. Tick **Overwrite my edits too**
   to replace edited files as well.
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
