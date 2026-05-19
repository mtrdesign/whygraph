# whygraph

Rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph): explains *why* code exists, not just what it does.

For each symbol, WhyGraph collects evidence from git history and GitHub (commits, blame, PRs, closing issues, callers/callees from CodeGraph), then exposes it to AI assistants over MCP plus an on-demand rationale (purpose, why, constraints, tradeoffs, risks) with a deterministic confidence score and persistent cache.

> **Status:** v1.x in progress on the `feature/scan-and-scoring` branch. MCP surface (resources, tools, prompts) is functional. The `/whygraph-plan` slash command + fan-out/fan-in planner subagents shipped in v1.3.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — Python toolchain. Installs Python 3.11+ automatically.
- **git** — repo history is the primary evidence source.
- **[`gh` CLI](https://cli.github.com/)**, authenticated (`gh auth login`) — required only if your repo is on GitHub. Without it, the GitHub crawl phase is skipped silently.
- **Node ≥ 22** — required by CodeGraph (used for graph queries). `whygraph init` bootstraps this for you via nvm if needed.
- **`claude` CLI** — required only for the LLM diff-description phase of `whygraph scan` and for `whygraph_rationale_brief`. Both phases skip cleanly if the CLI is missing. Defaults to your Claude.ai subscription billing.

## Quickstart

In the repository you want to analyse:

```bash
# 1. Bootstrap CodeGraph (Node ≥ 22, then runs `codegraph init -i`).
uv run --project /absolute/path/to/whygraph whygraph init

# 2. Scan: walks git history, fetches PRs/issues, runs TF-IDF scoring,
#    generates an LLM diff description per commit. Writes to
#    .whygraph/whygraph.db in the current repo.
uv run --project /absolute/path/to/whygraph whygraph scan

# 3. Verify the MCP server can launch.
uv run --project /absolute/path/to/whygraph whygraph-mcp   # Ctrl-C to exit
```

The full scan touches every commit on the default branch. On large or remote-heavy repos you may want to bound the LLM phase only:

```bash
# Run scan + LLM only on the 50 most recent commits. Other phases
# (git crawl, GitHub fetch, TF-IDF scoring) still cover full history.
uv run whygraph scan --llm-recent 50
```

## Installation

There are two ways to make WhyGraph available to a project: as a **Claude Code plugin** (recommended for AI-assistant use) or as a **standalone CLI** (for one-off scans, CI jobs, scripting).

### As a Claude Code plugin

From any project where you want WhyGraph available to Claude Code:

```
/plugin marketplace add /absolute/path/to/whygraph
/plugin install whygraph@whygraph
```

(Once published, replace the local path with `cvetty/whygraph`.)

After install, the `whygraph` MCP server is launched on demand by Claude Code via `uv run --project <plugin-checkout> whygraph-mcp`. Verify with `/mcp`.

### As a standalone CLI

```bash
uv tool install /absolute/path/to/whygraph
whygraph version
whygraph init
whygraph scan
```

This puts `whygraph` and `whygraph-mcp` on your `PATH`, independent of any Claude Code plugin.

## Wire WhyGraph into your editor

WhyGraph's MCP server (`whygraph-mcp`) is a standalone console script, so any LLM client that speaks MCP can use it. `whygraph init --client X` writes the right snippet to the right file for each supported client.

Run from the repo you want WhyGraph to analyse:

```bash
whygraph init                          # DB only, no client wiring (safe default)
whygraph init --client claude          # writes .mcp.json at repo root
whygraph init --client cursor          # writes .cursor/mcp.json
whygraph init --client vscode          # writes .vscode/mcp.json (alias: copilot)
whygraph init --client codex           # prints snippet for ~/.codex/config.toml
whygraph init --client claude-desktop  # prints snippet for Claude Desktop config
whygraph init --client X --print       # prints, never writes
whygraph init --list-clients           # show all supported clients + paths
```

**Project-scoped clients** (Claude Code, Cursor, VS Code / Copilot) get a config file written inside the repo so you can commit it — every contributor's editor picks it up automatically. **User-scoped clients** (Codex, Claude Desktop) are print-only: the command emits the snippet and tells you where to paste it, so WhyGraph never silently edits files outside the repo.

For Claude Code specifically, `--client claude` only wires the MCP server. To also get the `/whygraph-plan` slash command, skills, and planner subagents, install the Claude Code plugin as well (see [As a Claude Code plugin](#as-a-claude-code-plugin) above).

## CLI commands

| Command | Purpose |
|---|---|
| `whygraph version` | Print installed package version. |
| `whygraph init [-y]` | Bootstrap CodeGraph in the current repo. Detects/installs Node ≥ 22 via nvm if needed, then runs `codegraph init -i`. |
| `whygraph scan` | Walk first-parent history and populate `.whygraph/whygraph.db`: commits + GitHub PRs/issues + TF-IDF scoring + per-commit LLM diff descriptions. Idempotent. |
| `whygraph render [--out PATH] [--open] [--depth N]` | Render a self-contained HTML viewer of the CodeGraph + WhyGraph data. Single file, vendored Cytoscape, opens with double-click. Cached rationale only. `--depth N` (1–4, default 1) caps which nodes get a populated detail block — fast first paint at default 1 (modules only); pass `--depth 4` for full data. |
| `whygraph serve [--port 8765] [--open]` | Long-running localhost viewer with on-demand rationale generation. Same UI as `render`, plus a "Generate rationale" button on uncached nodes. |
| `whygraph-mcp` | Launch the FastMCP stdio server. Used by `.mcp.json` in the plugin and by MCP clients. |

### `whygraph scan` flags

| Flag | Default | Purpose |
|---|---|---|
| `--no-score` | off | Skip TF-IDF scoring after data collection. |
| `--no-llm-description` | off | Skip the per-commit LLM diff-description phase entirely. |
| `--anthropic-key TEXT` | unset | If set, the `claude` subprocess uses API billing with this key. If omitted, the subprocess inherits a stripped env (no `ANTHROPIC_API_KEY`), forcing Claude.ai subscription billing. |
| `--llm-workers N` | `4` | Parallel `claude` subprocesses in the LLM phase. |
| `--llm-recent N` | unbounded | Limit the LLM phase to the most recent N commits on the default branch. Other phases still cover full history. |
| `--llm-model TEXT` | `claude-sonnet-4-6` | Model used by the `claude` subprocess in the LLM phase. Also persisted to `commits.llm_description_model` per row. Use Opus on small repos for higher-quality descriptions; default is Sonnet for throughput on large scans. |

## MCP surface

The plugin's `.mcp.json` launches `whygraph-mcp`, which registers:

### Resources

- `whygraph://repo/overview` — counts, scan freshness, scoring + LLM coverage, top contributors.
- `whygraph://commit/{sha}` — full commit row + linked PRs + closing issues.
- `whygraph://pr/{number}` — full PR row + closing issues.
- `whygraph://issue/{number}` — full issue row + closing PRs.

### Tools

- **`whygraph_evidence_for`** — historical evidence (commits + PRs + closing issues) for a code chunk. Pass `(path, line_start, line_end)` or `qualified_name` (CodeGraph resolves it to a file/line range — no graph traversal). Multi-narrative output: each commit ships `llm_description` + `subject` + `body` when each clears the harshness gate. For caller/callee context, query CodeGraph or Claude Code's Explore agent separately.
- **`whygraph_search`** — LIKE-match query across commits/PRs/issues, ranked by TF-IDF.
- **`whygraph_velocity_summary`** — per-author commit velocity or per-path-prefix touch counts over a window. Author resolution goes through the `authors` identity table (replaces the old email-localpart heuristic).
- **`whygraph_window`** — generic windowed query over the scan DB. Filters: `since` / `until` (ISO date or relative shorthand `30d`/`3m`/`1y`), `kinds` (`commit` / `pr` / `issue`), `author` (login | email | name → resolved via `authors`), `path_prefix`, `label`, `state` (`merged` | `open` | `closed`). Returns time-ordered rows; the data spine for the analytics prompts below.
- **`whygraph_rationale_brief`** — generates the 5-section rationale card (purpose / why / constraints / tradeoffs / risks + confidence) by feeding the evidence bundle to a `claude` subprocess. **Cached** in the scan DB by `(target + bundle content + model + prompt version)` — re-invocation on unchanged code is a sub-millisecond DB read. Pass `force_refresh=True` to bypass.

### Prompts

Orchestration recipes that wire the tools above into common workflows:

- `explain_change` — pre-edit rationale for a code chunk.
- `debug_history` — find historical candidate causes for a bug symptom.
- `team_pulse` — per-author + per-path velocity over a rolling window.
- `changelog(since, until, scope?)` — themed markdown changelog of merged PRs in a date window.
- `feature_timeline(since, until)` — Mermaid `timeline` of merged PRs and issues opened in the window.
- `user_profile(identity, since, until)` — per-user contribution profile (commits, PRs, areas owned, issues closed).
- `whygraph_plan(task)` — composes search → CodeGraph (for symbol resolution) → rationale_brief into an ordered implementation plan.

### Composition with CodeGraph

WhyGraph deliberately does not expose graph-traversal tools. The split:

| Layer | Owns |
|---|---|
| **CodeGraph** (its MCP server / Claude's Explore agent) | "what is connected to what" — `findUsages`, `getCallers`, `find_symbols`, type hierarchy |
| **WhyGraph** | "why does this exist + when did it change" — evidence, rationale, windowed analytics |

The `whygraph_plan` prompt and the `/whygraph-plan` slash command both explicitly delegate symbol resolution to CodeGraph. For ad-hoc traversal mid-conversation, agents should call CodeGraph's tools directly.

### Slash command

`/whygraph-plan <task description> [--shallow|--deep] [--no-questions]` — produces a step-by-step implementation plan grounded in CodeGraph (impact) and WhyGraph (rationale). The plugin ships three subagents:

- **`whygraph-planner`** — orchestrator. Builds the working set via CodeGraph, warms the rationale cache, then either writes a plan single-pass (small scope) or fans out.
- **`whygraph-researcher`** — fan-out worker, instantiated three times in parallel with one of three dimensions: `impact` (blast radius via CodeGraph callers), `constraints_risks` (verbatim from rationale cards), `prior_art` (similar past PRs via `whygraph_search` / `whygraph_window`).
- **`whygraph-synthesizer`** — fan-in combiner. Folds the three reports into the final plan markdown with a confidence floor = lowest researcher confidence.

Auto-mode heuristic: single-pass when working set < 5 nodes OR > 60% of rationale cards are empty/low-confidence; fan-out otherwise. `--shallow` and `--deep` override.

### Skill

`plan-change` — auto-trigger description that nudges the user toward `/whygraph-plan` on planning-shaped prompts (e.g. *"plan how to refactor X"*, *"design a migration for Y"*). Suggestion-only; never auto-runs the planner — the slash command is the user's opt-in cost gate.

## HTML viewer

```bash
whygraph render --open    # static, self-contained HTML
whygraph serve --open     # localhost viewer with on-demand rationale
```

`render` writes `.whygraph/whygraph.html` — a single self-contained file (Cytoscape.js vendored inline) with three tabs:

- **Graph** — Cytoscape view of CodeGraph nodes/edges. Nodes are coloured by hierarchy level (Modules / Classes / Methods / Leaves); the slider buttons double as a legend. Cached rationale is shown as a green border. A **level slider** in the top bar controls how deep the graph displays — defaults to "Modules" for fast first paint. Edges aggregate up to the nearest visible ancestor when deeper levels are hidden. Click a node → side panel with top contributors (from blame), per-month activity, recent commits + linked PRs/issues, and the cached rationale card if any. Nodes deeper than the rendered `--depth` show a "re-render with `--depth N`" placeholder when clicked.
- **Dashboard** — repo overview (commits/PRs/issues counts), top contributors over the last 90 days, hottest path-prefixes, monthly activity bars.
- **Authors** — list of identities (resolved through the `authors` table); click → recent activity over the last 180 days.

`serve` starts a localhost-only HTTP server (default `127.0.0.1:8765`) with the same UI plus a "Generate rationale" button on uncached nodes. The button calls `whygraph_rationale_brief` server-side (~30s on first call, then cached); subsequent `whygraph render` runs include the newly-cached rationale in the static dump.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `WHYGRAPH_DB` | `<repo>/.whygraph/whygraph.db` | Where WhyGraph stores its evidence + rationale cache. Used by both the CLI and the MCP tools. |
| `CODEGRAPH_DB` | `<repo>/.codegraph/codegraph.db` | CodeGraph SQLite location. Required for `qualified_name` targeting on `whygraph_evidence_for`. |
| `ANTHROPIC_API_KEY` | unset | Honoured by the `claude` subprocess only when `--anthropic-key` is passed (or the equivalent tool argument). Not a runtime switch by itself. |

## Layout

```
.
├── .claude-plugin/marketplace.json     # single-plugin marketplace
├── plugins/whygraph/
│   ├── .claude-plugin/plugin.json      # plugin manifest
│   ├── .mcp.json                       # MCP server launch config
│   ├── commands/whygraph-plan.md       # /whygraph-plan slash command
│   ├── agents/                         # planner / researcher / synthesizer
│   └── skills/plan-change/SKILL.md     # auto-suggest skill
├── src/whygraph/
│   ├── cli.py                          # `whygraph` CLI (init, scan, version)
│   ├── init.py                         # CodeGraph bootstrap (nvm + codegraph init -i)
│   ├── mcp_server.py                   # FastMCP stdio server (resources/tools/prompts)
│   ├── mcp_queries.py                  # composite SQL for the MCP layer
│   ├── backend.py                      # GraphBackend Protocol + SqliteCodegraphBackend
│   ├── llm_subprocess.py               # `claude` CLI invocation helpers
│   └── scan/
│       ├── runner.py                   # parallel crawler orchestration
│       ├── git.py / github.py          # data sources
│       ├── db.py                       # WhyGraph SQLite schema + migrations
│       ├── scoring.py                  # TF-IDF + ValueGate
│       ├── authors.py                  # identity dedup + resolver
│       └── llm_descriptions.py         # per-commit diff-description phase
├── tests/
└── pyproject.toml                      # uv-managed
```

## Develop

```bash
uv sync                       # bootstrap .venv and install deps
uv run pytest                 # full test suite
uv run pytest tests/test_smoke.py::test_imports   # single test
uv run whygraph version       # CLI sanity check
uv run whygraph-mcp           # launch MCP server on stdio
```

If `uv` fails with `UnknownIssuer` SSL errors off-VPN, prefix with `SSL_CERT_FILE= ` (works around a corp-only cert bundle).

### Debug the MCP server with MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the official web UI for poking at a stdio MCP server — list tools, call them with custom args, see raw responses, tail stderr.

```bash
npx @modelcontextprotocol/inspector uv run whygraph-mcp
```

Open the printed `http://localhost:…` URL with the one-time auth token. Use **Reconnect** to pick up code changes.
