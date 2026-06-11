# whygraph

Rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph): explains *why* code exists, not just what it does.

For each symbol, WhyGraph collects evidence from git history and GitHub (commits, blame, PRs, closing issues, callers/callees from CodeGraph), then exposes it to AI assistants over MCP plus an on-demand rationale (purpose, why, constraints, tradeoffs, risks) with a persistent cache.

> **Status:** v1.x in progress on the `feature/scan-and-scoring` branch. MCP surface (resources, tools, prompts) is functional. The `/whygraph-plan` slash command + fan-out/fan-in planner subagents shipped in v1.3.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — Python toolchain. Installs Python 3.11+ automatically.
- **git** — repo history is the primary evidence source.
- **Docker** *(native installs only)* — when no `codegraph` binary is on PATH, `whygraph scan` runs CodeGraph inside the WhyGraph image to index the repo. No host Node install needed. The pure-Docker install (below) already runs everything in that image.
- **[`gh` CLI](https://cli.github.com/)**, authenticated (`gh auth login`) — required only if your repo is on GitHub. Without it, the GitHub crawl phase is skipped silently.
- **`claude` CLI** *or* `ANTHROPIC_API_KEY` — needed for the LLM diff-description phase of `whygraph scan` and for `whygraph_rationale_brief`. Both phases skip cleanly if neither is available. The `claude` CLI defaults to your Claude.ai subscription billing.

## Quickstart

Install WhyGraph once (see [Installation](#installation) below), then in the repository you want to analyse:

```bash
# 1. Bootstrap WhyGraph + CodeGraph (via Docker; idempotent — re-runs are no-ops).
whygraph init

# 2. Scan: walks git history, fetches PRs/issues, runs TF-IDF scoring,
#    generates an LLM diff description per commit. Writes to
#    .whygraph/whygraph.db in the current repo.
whygraph scan

# 3. Verify the MCP server can launch.
whygraph-mcp   # Ctrl-C to exit
```

The full scan touches every commit on the default branch. On large or remote-heavy repos you may want to bound the LLM phase only:

```bash
# Run scan + LLM only on the 50 most recent commits. Other phases
# (git crawl, GitHub fetch, TF-IDF scoring) still cover full history.
whygraph scan --llm-recent 50
```

## Installation

WhyGraph follows a **one-global-install / use-anywhere** model — like `npx`, but for Python. You install the package once on your machine; that puts the `whygraph` and `whygraph-mcp` console scripts on your `PATH`. Then `whygraph init --agent <name>` wires up each individual project so its agent can launch the MCP server.

Pick whichever install path fits where the project is in its lifecycle:

### From PyPI (stable releases)

```bash
uv tool install whygraph        # or: pipx install whygraph
```

> **Status:** WhyGraph is not yet published to PyPI. Use one of the GitHub or local-checkout paths below until v1 ships.

### From GitHub (latest / pre-release)

For unreleased features on `main`, a specific feature branch, or a tag:

```bash
# Latest from main:
uv tool install "git+https://github.com/mtrdesign/whygraph.git"

# A specific branch (e.g. an in-flight feature):
uv tool install "git+https://github.com/mtrdesign/whygraph.git@feature/scan-and-scoring"

# A specific tag (once tagged):
uv tool install "git+https://github.com/mtrdesign/whygraph.git@v1.3.0"
```

Re-running upgrades in place. To switch refs, add `--force` (or `uv tool uninstall whygraph` first). `pipx` accepts the same `git+https://…` URLs.

### From a local checkout (contributors)

```bash
git clone https://github.com/mtrdesign/whygraph.git
uv tool install --editable ./whygraph
```

`--editable` lets your local edits show up immediately, without reinstalling.

### Verify

```bash
whygraph version
which whygraph-mcp
```

Both should resolve to the global tool install (under `~/.local/bin/` or `uv`'s shim directory).

### Wire each project

```bash
cd /path/to/your-project
whygraph init --agent claude     # Claude Code: writes .mcp.json + drops bundled assets in .claude/
```

`whygraph init --agent claude` writes `.mcp.json` at the repo root and copies the bundled agent / command / skill markdown into `<project>/.claude/agents`, `/.claude/commands`, `/.claude/skills`. The `.mcp.json` references `whygraph-mcp` by bare command name, so the same checked-in config works for every teammate who has WhyGraph installed globally — no absolute paths to scrub. Re-running is safe — pre-existing files are left alone; pass `--force` to overwrite, `--no-install-assets` to skip the asset copy entirely.

### Migration from the Claude Code plugin

Earlier versions shipped via the Claude Code plugin marketplace. That path is gone — there is no more `plugins/whygraph/` or `.claude-plugin/marketplace.json`, and an old `/plugin install whygraph@whygraph` will fail. To migrate:

```
# In your Claude Code session:
/plugin uninstall whygraph@whygraph
/plugin marketplace remove whygraph

# Then in each project where you want WhyGraph:
whygraph init --agent claude
```

## Wire WhyGraph into your editor

WhyGraph's MCP server (`whygraph-mcp`) is a standalone console script, so any LLM agent that speaks MCP can use it. `whygraph init --agent X` writes the right snippet to the right file for each supported agent.

Run from the repo you want WhyGraph to analyse:

```bash
whygraph init                          # preflight + WhyGraph DB + example config (no agent wiring)
whygraph init --skip-preflight         # skip the host-tool diagnostics (CI escape hatch)
whygraph init --agent claude          # all of the above + writes .mcp.json + populates .claude/
whygraph init --agent cursor          # writes .cursor/mcp.json
whygraph init --agent vscode          # writes .vscode/mcp.json (alias: copilot)
whygraph init --agent codex           # prints snippet for ~/.codex/config.toml
whygraph init --agent X --print       # prints the MCP snippet, never writes it
whygraph init --agent claude --no-install-assets   # MCP only, skip .claude/ copy
whygraph init --agent claude --force               # overwrite existing .claude/* files
whygraph init --list-agents            # show all supported agents + paths (no preflight, no bootstrap)
```

**Project-scoped agents** (Claude Code, Cursor, VS Code / Copilot) get a config file written inside the repo so you can commit it — every contributor's editor picks it up automatically. **User-scoped agents** (Codex, Claude Desktop) are print-only: the command emits the snippet and tells you where to paste it, so WhyGraph never silently edits files outside the repo.

`--agent claude` is the only path that also installs the `/whygraph-plan` slash command, the `plan-change` skill, and the planner / researcher / synthesizer subagents — these only make sense for Claude Code, so they ship as `.claude/*` markdown rather than as part of the MCP surface.

## Run with Docker (only Docker required)

Don't want Python, Node, `gh`, and CodeGraph on your machine? WhyGraph ships as a self-contained image. The host needs **only Docker** — install a tiny shim, then it's the same `init` / `scan` as a native install:

```bash
curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/main/scripts/install.sh | sh

cd your-repo
whygraph init      # bootstrap WhyGraph DB + write config (+ optional --agent wiring)
whygraph scan      # crawl history + build/refresh CodeGraph index + LLM descriptions
```

`install.sh` drops a `whygraph` (and `whygraph-mcp`) shim on your `PATH` that runs the published image (`ghcr.io/mtrdesign/whygraph`) against the current directory — `docker run --rm -v "$PWD:/workspace" … whygraph "$@"`. The container is ephemeral per command: no compose, no `docker exec`, nothing to start or stop.

- **Everything is in the image** — Python + WhyGraph, `git`, the GitHub CLI, and Node + the CodeGraph CLI. CodeGraph indexes from the in-image binary, so there's no docker-in-docker.
- **Per-project config just works.** Each command runs against the current repo, reading that repo's own `whygraph.toml`, `.whygraph/`, and `.codegraph/`. Generated files are written back owned by your user.
- **GitHub token** goes in `[scan].token` of the repo's `whygraph.toml` (gitignored). The shim also passes through `GH_TOKEN` / `GITHUB_TOKEN` and `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` from your environment.

Build the image yourself instead of pulling (e.g. while developing) with `docker build -f docker/whygraph/Dockerfile -t whygraph:latest .`, then `WHYGRAPH_IMAGE=whygraph:latest whygraph scan`.

### Use it in your editor (MCP), still only Docker

The MCP server is containerized too — `install.sh` drops a `whygraph-mcp` shim alongside `whygraph`, so there's nothing extra to install on the host. Wire your editor from inside the repo:

```bash
whygraph init --agent claude     # writes .mcp.json (also: --agent cursor / vscode)
```

The generated `.mcp.json` launches `whygraph-mcp` by bare command name; your editor resolves it to the shim, which starts a per-session container (`docker run -i … whygraph-mcp`) speaking MCP over stdio. It reads the repo's `.whygraph/` + `.codegraph/` over the same `/workspace` mount the scan writes to — so the editor and the scan share one on-disk source of truth.

- Reading cached rationale / evidence needs **no credentials**.
- On-demand rationale *generation* uses `ANTHROPIC_API_KEY` from the editor's environment (the shim passes it through) or the repo's `whygraph.toml [llm.*] api_key`.

## CLI commands

| Command | Purpose |
|---|---|
| `whygraph version` | Print installed package version. |
| `whygraph init` | Run preflight diagnostics (git / gh / LLM credential), bootstrap the WhyGraph DB, and write the example config + `.gitignore` entries. Pass `--agent <name>` to also wire MCP for an editor. Idempotent. Does not index CodeGraph — that happens on `scan`. |
| `whygraph scan` | Build or refresh the CodeGraph index (`.codegraph/codegraph.db`), then walk first-parent history and populate `.whygraph/whygraph.db`: commits + GitHub PRs/issues + TF-IDF scoring + per-commit LLM diff descriptions. Idempotent. `--no-codegraph` skips the index refresh; `--no-remote` skips the PR/issue crawl for a fast, offline, git-only scan. |
| `whygraph hooks install / uninstall / status` | Opt-in git hooks (`post-commit` / `post-merge` / `post-rewrite`) that keep WhyGraph current as you commit — see [Keep it fresh automatically](#keep-it-fresh-automatically). |
| `whygraph render [--out PATH] [--open] [--depth N]` | Render a self-contained HTML viewer of the CodeGraph + WhyGraph data. Single file, vendored Cytoscape, opens with double-click. Cached rationale only. `--depth N` (1–4, default 1) caps which nodes get a populated detail block — fast first paint at default 1 (modules only); pass `--depth 4` for full data. |
| `whygraph serve [--port 8765] [--open]` | Long-running localhost viewer with on-demand rationale generation. Same UI as `render`, plus a "Generate rationale" button on uncached nodes. |
| `whygraph-mcp` | Launch the FastMCP stdio server. Referenced by the `.mcp.json` files `whygraph init --agent X` writes into each project. |

### Keep it fresh automatically

Don't want to re-scan by hand? Install git hooks once and new commits refresh WhyGraph + CodeGraph on the fly:

```bash
whygraph hooks install      # opt-in; uninstall / status also available
```

This wires `post-commit`, `post-merge`, and `post-rewrite` to run an incremental scan **in the background** — git history + a CodeGraph `sync`, with **no LLM and no remote calls**, so commits stay instant and the scan is offline and token-free (LLM descriptions still backfill lazily; run a full `whygraph scan` for PRs/issues + descriptions). Rapid commits coalesce (single-flight), and an existing hook of your own is appended to, never overwritten. The hooks call whatever `whygraph` is on your `PATH` — so they work with both the Docker shim and a native install.

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

The `.mcp.json` written by `whygraph init --agent claude` (and the equivalents for other agents) launches `whygraph-mcp`, which registers:

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

`/whygraph-plan <task description> [--shallow|--deep] [--no-questions]` — produces a step-by-step implementation plan grounded in CodeGraph (impact) and WhyGraph (rationale). `whygraph init --agent claude` drops three subagents under `.claude/agents/`:

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
├── src/whygraph/
│   ├── cli.py                          # `whygraph` CLI (init, scan, version)
│   ├── agents.py                       # agent registry + MCP snippet writers
│   ├── assets.py                       # .claude/ asset installer (skip-if-exists + --force)
│   ├── assets/claude-code/             # bundled Claude Code assets (shipped in the wheel)
│   │   ├── agents/                     # planner / researcher / synthesizer / implementor
│   │   ├── commands/                   # /rationale, /whygraph-plan, /whygraph-implement
│   │   └── skills/                     # ask-why / plan-change / pre-edit / implement-plan
│   ├── init.py                         # WhyGraph DB + config + agent MCP wiring
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

If `uv` fails with `UnknownIssuer` SSL errors off-VPN, prefix with `SSL_CERT_FILE= ` (works around a corp-only cert bundle) — this applies to `make` targets too, e.g. `SSL_CERT_FILE= make sync`.

A `Makefile` wraps the common dev tasks; run `make` to list them — `make sync`, `make test`, `make scan`, `make db` / `make db-down`, `make inspect`.

### Browse the databases

WhyGraph is developed by running it against its own repo, so it helps to eyeball the two SQLite databases it touches — `.whygraph/whygraph.db` (its own evidence/rationale data) and `.codegraph/codegraph.db` (CodeGraph's symbol graph). `make db` brings up [DBGate](https://dbgate.org/) in Docker with both databases wired up as connections:

```bash
cp docker-compose.example.yml docker-compose.yml   # one-time; the copy is git-ignored
make db                                            # DBGate at http://localhost:8081
make db-down                                       # stop the viewer
```

Both databases appear in the DBGate sidebar; the CodeGraph one is opened read-only since CodeGraph rewrites it on re-index. Toggle the dark theme in DBGate's Settings — it persists across restarts.

### Debug the MCP server with MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the official web UI for poking at a stdio MCP server — list tools, call them with custom args, see raw responses, tail stderr.

```bash
make inspect                           # against this checkout
make inspect REPO=/path/to/other/repo  # against another repo's databases
```

`make inspect` needs Node ≥ 20 active — the same modern Node CodeGraph requires (`nvm use 22`). Open the printed `http://localhost:…` URL with the one-time auth token. Use **Reconnect** to pick up code changes.
