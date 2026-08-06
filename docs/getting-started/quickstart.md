# Quickstart

You've [installed WhyGraph](installation.md). Now point it at a repo. This is the happy path: init,
scan, wire an editor, sanity-check.

## 1. Initialize

From the repo you want to analyze:

```bash
whygraph init
```

On a terminal this runs a short guided setup - pick your agent, the analyze/rationale LLMs (with
optional API keys), the source-control provider (with an optional GitHub token), and whether to
install the [auto-rescan git hooks](../guide/scanning.md#keep-it-fresh) - then review a summary that
masks every secret and confirm. It creates `.whygraph/whygraph.db`, writes a commented
`whygraph.example.toml` (never any secrets) and a ready-to-run `whygraph.toml` (with the secrets you
entered), adds the right `.gitignore` entries, and reconciles `.git/hooks`. Every prompt is defaulted,
so a bare Enter accepts it. It's idempotent - run it again any time; an existing `whygraph.toml` is
only touched if you ask. It does *not* index CodeGraph yet; that's the next step.

Prefer no prompts? `whygraph init --yes` accepts every default without asking and writes a default
`whygraph.toml` if none exists. Off a TTY - pipes, CI, the git hooks - `init` also runs without
prompting, but there it refreshes only `whygraph.example.toml` and leaves `whygraph.toml` alone;
pass `--yes` when you want the non-interactive run to write it.

## 2. Scan

```bash
whygraph scan
```

`scan` walks your git history and, optionally, crawls the remote for PRs and issues; recovers the
original commits behind squash-merged PRs; resolves commit addresses into one row per person; and
writes a per-commit LLM description. The CodeGraph index refreshes in the background alongside all of
it. That fills `.whygraph/whygraph.db` with the evidence WhyGraph serves, and closes with a panel
summarizing each phase.

!!! note "The remote crawl is off by default"
    A fresh scan stays git-only and needs no token, because `[scan].provider` defaults to `"off"`. To
    pull PRs and issues, set `provider = "github"` (or `"auto"`) in `whygraph.toml`.

For a fast, offline pass - no remote calls, no LLM - skip both phases:

```bash
whygraph scan --no-remote --skip-analyze
```

Descriptions backfill lazily later, so this is a fine way to get started quickly. See
[Scanning your repo](../guide/scanning.md) for what each phase does.

!!! tip "Prefer a visual view?"
    Once you've scanned, `whygraph serve` opens a local web panel with two views: the
    [Explorer](../guide/playground.md) over the graph, evidence, and rationale, and a
    [chat assistant](../guide/chat.md) that answers questions about the repo by calling WhyGraph's
    tools. Browse it instead of - or alongside - your editor.

## 3. Wire your editor

Register the MCP server with your agent. For Claude Code:

```bash
whygraph init --agent claude
```

That writes `.mcp.json` at the repo root and copies the bundled assets into `.claude/`. Other agents
work the same way, each with its own config path and asset destination - `--agent cursor`,
`--agent vscode`, `--agent codex`. See [Wiring your editor](../guide/editors.md).

## 4. Sanity-check the server

```bash
whygraph-mcp   # Ctrl-C to exit
```

If it launches without error, your editor can launch it too. That's it - ask your assistant why a
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

-   :material-graph-outline:{ .lg .middle } __Explorer playground__

    ---

    Browse the graph, evidence, and rationale in a local web panel.

    [:octicons-arrow-right-24: Playground](../guide/playground.md)

</div>
