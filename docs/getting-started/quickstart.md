# Quickstart

You've [installed WhyGraph](installation.md). Now point it at a repo. This is the happy path: init,
scan, wire an editor, sanity-check.

## 1. Initialize

From the repo you want to analyze:

```bash
whygraph init
```

This creates `.whygraph/whygraph.db`, writes a commented `whygraph.example.toml`, and adds the right
`.gitignore` entries. It's idempotent — run it again any time. It does *not* index CodeGraph yet;
that's the next step.

## 2. Scan

```bash
whygraph scan
```

`scan` walks your git history, optionally crawls the remote for PRs and issues, refreshes the
CodeGraph index, and writes a per-commit LLM description. That fills `.whygraph/whygraph.db` with the
evidence WhyGraph serves.

!!! note "The remote crawl is off by default"
    A fresh scan stays git-only and needs no token, because `[scan].provider` defaults to `"off"`. To
    pull PRs and issues, set `provider = "github"` (or `"auto"`) in `whygraph.toml`.

For a fast, offline pass — no remote calls, no LLM — skip both phases:

```bash
whygraph scan --no-remote --no-llm-descriptions
```

Descriptions backfill lazily later, so this is a fine way to get started quickly. See
[Scanning your repo](../guide/scanning.md) for what each phase does.

## 3. Wire your editor

Register the MCP server with your agent. For Claude Code:

```bash
whygraph init --agent claude
```

That writes `.mcp.json` at the repo root and copies the bundled assets into `.claude/`. Other agents
work the same way — `--agent cursor`, `--agent vscode`, `--agent codex`. See
[Wiring your editor](../guide/editors.md) for each one's config path.

## 4. Sanity-check the server

```bash
whygraph-mcp   # Ctrl-C to exit
```

If it launches without error, your editor can launch it too. That's it — ask your assistant why a
function exists, and WhyGraph answers from history.

## Where to next

<div class="grid cards" markdown>

-   :material-lightbulb-on:{ .lg .middle } __Concepts__

    ---

    Evidence, rationale cards, and the CodeGraph split.

    [:octicons-arrow-right-24: Concepts](../guide/concepts.md)

-   :material-connection:{ .lg .middle } __Using WhyGraph__

    ---

    How an agent calls the tools mid-task.

    [:octicons-arrow-right-24: MCP usage](../guide/mcp-usage.md)

</div>
