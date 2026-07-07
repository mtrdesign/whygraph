# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

WhyGraph v1 is the Python implementation, now living on `main`. Live components: the MCP server (evidence tool, rationale tool with SQLite-backed content-addressable cache, repo / commit / PR / issue resources, orchestration prompts), the CLI (`init`, `scan`, `analyze`, `version`), and the `/whygraph-plan` slash command + fan-out/fan-in planner subagents. The earlier HTML render/serve viewer was removed during the III iteration migration and is not currently in the CLI. The original TypeScript POC was retired; pre-`85fe8b3` commit history covers the v0 design for archaeology.

Core architectural decisions that still apply — read these before adding architecture:

- **Graph backend abstraction.** A `GraphBackend` Python protocol (`get_node`, `get_callers`, `get_callees`, `find_symbols`, `walk_neighbors`) with `SqliteCodegraphBackend` as the first impl (reads CodeGraph's SQLite directly — no subprocess, no MCP roundtrip). Other backends (`JsonGraphifyBackend`, `MCPBackend`) drop in later without re-architecting.
- **Plugin shape, in order.** (1) MCP tools `whygraph_rationale_pre_edit_brief` and `whygraph_evidence_for`. (2) A `/whygraph-plan <task>` slash command that spawns a Plan subagent via the `Agent` tool with rationale cards **inlined at spawn time**. (3) Workers after the planner.
- **WhyGraph's MCP surface stays narrow** — rationale + evidence cards only. Users who want raw graph queries install the graph backend's own MCP server alongside.
- **Cache key must be content-addressable** (hash of `qualified_name + file_path`, not the backend's `node_id`) so cards survive a backend swap.

## Common commands

The project is uv-managed (Python ≥ 3.11, pinned via `.python-version`).

```bash
uv sync                       # bootstrap .venv and install deps
uv run pytest                 # all tests
uv run pytest tests/test_smoke.py::test_imports   # single test
uv run whygraph version       # CLI sanity check
uv run whygraph-mcp           # launch MCP server on stdio (Ctrl-C to exit)
```

A root `Makefile` wraps these plus dev-only tooling — `make` lists targets; `make db` / `make db-down` run a DBGate viewer for both databases (via `docker-compose.example.yml`), `make inspect` launches the MCP Inspector.

## Before pushing

CI (`ci-code-checks`) gates every PR on two parallel jobs: **lint** (`uv run ruff check src/ tests/` *and* `uv run ruff format --check src/ tests/` — both, not just the first) and **tests** (`uv run pytest`). Run all three locally before pushing or opening a PR:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/   # `ruff check` passing does NOT imply this passes
uv run pytest
```

If `ruff format --check` fails, run `uv run ruff format src/ tests/` to fix it in place, then re-run the check before pushing.

## Architecture

Top-level packages under `src/whygraph/`:

- `cli/` — Click group + one module per subcommand under `cli/commands/` (`init`, `scan`, `analyze`, `version`). `cli/__init__.py` assembles the group, configures logging once per invocation, and exports `main`. Shared console formatting lives in `cli/console.py`.
- `mcp/` — FastMCP stdio server. `server.py` builds the `FastMCP("whygraph")` instance and exposes `main()`; feature modules (`evidence.py`, `rationale.py`, `rationale_cache.py`, `targets.py`, `errors.py`) each register their tools via a `register(mcp)` function, so new MCP features land as new modules without growing a monolith.
- `core/` — cross-cutting helpers: `config` (env / project config), `logger` (logging setup), `shell` / `shell_command` (subprocess helpers), `utils`.
- `db/` — SQLite plumbing. `engine.py` + `bootstrap.py` set up the DB, `base.py` is the declarative base, `models/` holds the SQLModel classes, `migrations/` holds Alembic versions.
- `services/` — external integrations: `git/`, `github/`, `codegraph/` (reads CodeGraph's SQLite by `node_id`), `llm/` (Anthropic / OpenAI / Ollama subprocess wrappers).
- `scan/` — crawler orchestration. `crawler.py` drives `git_crawler.py`, `github_crawler.py`, and `analyze_crawler.py` per-source phases.
- `analyze/` — LLM-backed analysis. `description.py` / `llm_descriptor.py` produce per-commit diff descriptions; `rationale.py` / `rationale_generator.py` produce the 5-section rationale cards; `backfill.py` runs the lazy on-read backfill. Prompt templates live under `analyze/prompts/`.
- `agents.py` — registry of supported LLM agents (Claude Code, Cursor, VS Code / Copilot, Codex, Claude Desktop) and the per-agent MCP config wiring (`write_snippet` / `render_snippet`). `whygraph init --agent X` reads from here.
- `assets.py` + `assets/claude-code/` — bundled Claude Code assets (agents, commands, skills) copied into a project's `.claude/` by `whygraph init --agent claude`. Loaded at runtime via `importlib.resources.files("whygraph") / "assets" / "claude-code"`; same packaging precedent as `analyze/prompts/`.
- `__main__.py` — enables `python -m whygraph`.

Console scripts in `pyproject.toml`: `whygraph` → `cli:main`, `whygraph-mcp` → `mcp.server:main`. Both must keep working — `.mcp.json` files written by `whygraph init` and the `uv tool install` path depend on them.

## Install path

WhyGraph installs **per project**, per agent:

1. `uv tool install whygraph` (or `pipx install whygraph`) so `whygraph` and `whygraph-mcp` are on `PATH`.
2. From the target repo: `whygraph init --agent <name>` — `--agent claude` writes `.mcp.json` and copies the bundled assets into `.claude/`; other agents (cursor / vscode / codex / claude-desktop) just wire their MCP config. Every agent's asset install also drops a **CodeGraph usage-guidance block** into that agent's always-on instructions (`.claude/CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` via append-merge; an `alwaysApply: true` rule for Cursor) so the agent prefers the `codegraph_*` tools over grep without the user running CodeGraph's own installer.

There is no Claude Code marketplace install; `whygraph init --agent claude` is the only path. The bundled assets are version-controlled in this repo under `src/whygraph/assets/claude-code/` — that is the source of truth, the wheel ships them, and a re-run of `whygraph init` brings a project's `.claude/` up to date (use `--force` to overwrite local edits).

## Docker delivery (the default install)

WhyGraph ships as a self-contained image so a developer needs **only Docker** on the host — no Python / Node / gh / codegraph install. The whole UX is three steps:

```bash
curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/main/scripts/install.sh | sh
whygraph init     # in a repo
whygraph scan
```

`scripts/install.sh` drops a ~10-line `whygraph` (and `whygraph-mcp`) **shim** on PATH that wraps `docker run --rm -v "$PWD:/workspace" -w /workspace <image> whygraph "$@"` and pulls `ghcr.io/mtrdesign/whygraph`. The container is **ephemeral per command** — there is no compose, no `docker exec`, no long-running container. The image is built from `docker/whygraph/Dockerfile` (base `python:3.12-slim` + git + gh + Node 22 + pinned CodeGraph CLI + WhyGraph) and published by `.github/workflows/publish-whygraph-image.yml`.

Invariants that keep the shim correct — preserve them:

- **Each command is a fresh process against `cwd`.** `get_config()` is globally memoized and config / DB-path discovery walks up from `cwd` to the `.git` root (`core/__init__.py`, `db/engine.py`); the shim mounts the repo at `/workspace` and runs there, so each `whygraph` picks up that repo's own `whygraph.toml`, `.whygraph/`, `.codegraph/`. Ephemeral `docker run` makes this automatic — don't reintroduce an in-process loop that scans multiple repos without resetting config.
- **Host-user file ownership.** The shim runs `--user "$(id -u):$(id -g)" -e HOME=/tmp` so `.whygraph/` and `.codegraph/` come back owned by the host user and git sees matching ownership (no "dubious ownership"). Keep these when editing the shim.
- **GitHub token is per-project.** `[scan].token` in each repo's `whygraph.toml` (gitignored, never committed) is exported as `GH_TOKEN` for that scan's `gh` calls (`cli/commands/scan.py:_apply_github_token`), falling back to ambient `GH_TOKEN` / `GITHUB_TOKEN` (the shim passes those through). Do **not** bake a token into the image or assume a container-wide token.

CodeGraph indexing belongs to **`whygraph scan`, not `whygraph init`** — `init` only bootstraps the WhyGraph DB / config / agent wiring (and its preflight no longer requires `docker`). `whygraph scan` builds or refreshes the index each run — `codegraph init -i` on first run, `codegraph sync -q` thereafter — gated by `--codegraph/--no-codegraph`. CodeGraph runs from the **in-image `codegraph` binary** (no docker-in-docker, no host socket): `bootstrap.py` (`services/codegraph/`) prefers the local binary and only falls back to `docker run` on native (`uv`/`pipx`) hosts without it. There is a **single image** — `ghcr.io/mtrdesign/whygraph` — and that fallback runs `codegraph` inside it (`docker run … whygraph codegraph …`); there is no separate codegraph image.

### Auto-rescan git hooks

`whygraph hooks install` (opt-in; `cli/commands/hooks.py`) wires `post-commit` / `post-merge` / `post-rewrite` to keep the DBs current as the developer works — no daemon. Each hook execs a shared helper (`.whygraph/hooks/whygraph-scan`, gitignored) that runs `whygraph scan --no-llm-descriptions --no-remote` (git history + `codegraph sync` only — fast, offline, no token; LLM descriptions stay on lazy backfill). The helper is **detached** (commits return instantly) and **single-flight + coalescing** (portable `mkdir` lock + a `pending` flag, since macOS has no `flock`), so rapid commits neither stack nor drop the latest `HEAD`. Installs are **sentinel-guarded** (`# >>> whygraph managed >>>`) and append to a foreign hook rather than clobber it. The `--no-remote` flag on `scan` exists for this path; `db/engine.py` sets `PRAGMA busy_timeout` so a background rescan and a manual scan don't collide.

Deferred (net-new, not built yet): a project registry for cross-repo orchestration, a persistent/server mode, and per-branch CodeGraph/WhyGraph databases.

## Conventions

- **Don't add new top-level dirs** without updating `[tool.hatch.build.targets.wheel].packages` in `pyproject.toml` (currently `["src/whygraph"]`).
- **Tests live in `tests/`** (configured via `[tool.pytest.ini_options].testpaths`). `test_smoke.py` asserts the package imports and the MCP server is named `"whygraph"` — preserve both invariants when restructuring.
- **Companion repo:** CodeGraph upstream is `colbymchenry/codegraph`. WhyGraph reads its SQLite output and joins by `node_id`. Schema reference: tables `nodes`, `edges`, `files`, `nodes_fts`, `unresolved_refs`.
- **Docstrings.** All public modules, classes, and functions in `src/whygraph/` use [NumPy-style docstrings](https://numpydoc.readthedocs.io/en/latest/format.html) — sections `Parameters`, `Returns`, `Raises`, `Attributes`, `Notes`, `Examples` as applicable. Private helpers (`_foo`) get a one-line summary unless behavior is non-obvious. This overrides the global "no multi-line docstrings" default for this project. Do **not** retrofit docstrings as drive-by changes on unrelated PRs — that's a focused, standalone change.
- **Intra-package imports use the relative form.** Inside `src/whygraph/`, when a module imports from another module in the **same package**, use the relative path (`from .commit import Commit`), not the absolute (`from whygraph.services.git.commit import Commit`). Cross-package imports inside `src/whygraph/` (e.g. a `services/git/` module importing from `whygraph.core`) stay absolute. Tests and console-script entry points always use absolute imports. Don't retrofit existing absolute intra-package imports as drive-by changes — that's a focused standalone PR.

## Working principles

### 1. Think before coding

Surface assumptions before writing code. If a request has multiple plausible interpretations, name them and ask — don't pick silently.

- "Add caching for rationale cards" — in-memory per-process? On-disk under `~/.cache/whygraph`? Keyed by `node_id` or by `qualified_name + file_path`? The cache key is settled (content-addressable, so cards survive a backend swap), but TTL, location, and invalidation are open. Ask before implementing.
- "Make graph queries faster" — lower latency on a single `get_callers`, higher throughput across many calls, or perceived speed via streaming partial results? Each implies a different change.

### 2. Simplicity first

Solve today's problem with the smallest thing that works. Add abstraction when a second concrete case forces it, not in anticipation.

- For a one-off SQLite read, a function in `sqlite_codegraph_backend.py` is enough. Don't introduce a `QueryBuilder` class until a second backend actually needs to share query logic.
- The `GraphBackend` protocol is the *exception that proves the rule*: it's introduced up-front because three concrete backends are already named (`SqliteCodegraphBackend`, `JsonGraphifyBackend`, `MCPBackend`). Without that, a single backend wouldn't justify a protocol.

### 3. Surgical changes

When fixing a bug or adding a feature, change only the lines that the task requires. Don't reformat, retype, or rename code you happen to be reading.

- Fixing a crash in `get_callers` doesn't license rewriting the surrounding query, adding type hints to neighbours, or "while I'm here" docstring passes.
- Match the existing style — quote choice, import grouping, error-handling shape — even if it's not your preference. Style drift in a fix PR makes the diff impossible to review.

### 4. Goal-driven execution

Define a verifiable success criterion before changing code. Prefer small, independently-verifiable steps over a single large change.

- "Wire up `whygraph_rationale_pre_edit_brief`" → step 1: register the tool with a stub return and confirm it appears in `uv run whygraph-mcp` output; step 2: thread a `GraphBackend` lookup through it with a fixture; step 3: add real rationale assembly. Each step has its own test.
- For bug fixes, write the failing test first. If you can't reproduce the bug in a test, you don't yet understand it.

### Anti-patterns at a glance

| Principle              | Anti-pattern                                                  | Fix                                                       |
| ---------------------- | ------------------------------------------------------------- | --------------------------------------------------------- |
| Think before coding    | Silently picks one interpretation and ships it                | List the interpretations, ask which one                   |
| Simplicity first       | Protocol + factory + config dataclass for one concrete case   | One function/class until a second case appears            |
| Surgical changes       | Reformats quotes / adds type hints alongside a one-line fix   | Touch only the lines the task requires                    |
| Goal-driven execution  | "I'll review and improve the module"                          | "Failing test for X → make it pass → no regressions"      |

### Key insight

Premature complexity isn't obviously wrong — it usually follows recognisable patterns and "best practices". The problem is timing: complexity added before it's needed costs comprehension, review, test surface, and bug count, and is usually wrong about what was actually needed once a second case arrives. Solve today's problem simply; refactor when a real second case forces the abstraction.
