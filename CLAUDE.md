# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

WhyGraph v1 is the Python implementation, now living on `main`. Live components: the MCP server (evidence tool, rationale tool with SQLite-backed content-addressable cache, repo / commit / PR / issue resources, orchestration prompts), the CLI (`init`, `scan`, `analyze`, `serve`, `version`), the `whygraph serve` playground (the Explorer graph view, and the **Chat** assistant — an in-house streaming tool-calling harness over OpenRouter / OpenAI / Anthropic / DeepSeek, with sessions in the WhyGraph DB; see `plans/chat-assistant-plan.md`), and the `/whygraph-plan` slash command + fan-out/fan-in planner subagents. The earlier *static HTML* render viewer was removed during the III iteration migration; the current viewer is the React playground served by `whygraph serve`. The original TypeScript POC was retired; pre-`85fe8b3` commit history covers the v0 design for archaeology.

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

CI (`ci-code-checks`) gates every PR on two parallel jobs: **lint** (`uv run ruff check src/ tests/ scripts/` *and* `uv run ruff format --check src/ tests/ scripts/` — both, not just the first) and **tests** (`uv run pytest`). `scripts/` is linted but **not** collected by pytest (`testpaths` is `tests/`) — it holds live-provider tooling that must never gate CI. Run all three locally before pushing or opening a PR:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/   # `ruff check` passing does NOT imply this passes
uv run pytest
```

If `ruff format --check` fails, run `uv run ruff format src/ tests/ scripts/` to fix it in place, then re-run the check before pushing.

## Architecture

Top-level packages under `src/whygraph/`:

- `cli/` — Click group + one module per subcommand under `cli/commands/` (`init`, `scan`, `analyze`, `version`). `cli/__init__.py` assembles the group, configures logging once per invocation, and exports `main`. Shared console formatting lives in `cli/console.py`.
- `mcp/` — FastMCP stdio server. `server.py` builds the `FastMCP("whygraph")` instance and exposes `main()`; feature modules (`evidence.py`, `rationale.py`, `rationale_cache.py`, `targets.py`, `errors.py`) each register their tools via a `register(mcp)` function, so new MCP features land as new modules without growing a monolith.
- `core/` — cross-cutting helpers: `config` (env / project config), `logger` (logging setup), `shell` / `shell_command` (subprocess helpers), `utils`.
- `db/` — SQLite plumbing. `engine.py` + `bootstrap.py` set up the DB, `base.py` is the declarative base, `models/` holds the SQLModel classes, `migrations/` holds Alembic versions.
- `serve/` — the `whygraph serve` playground backend. `app.py` is the FastAPI factory (mounts `routes.py` at `/api` and `chat.py` at `/api/chat`, then the SPA from `static/`); `graphdata.py` shapes CodeGraph reads for the UI; `coverage.py` computes rationale coverage. **Every handler is a sync `def`** — FastAPI runs them in the threadpool, one `get_session()` and one `CodeGraph` handle per request. The React source lives in `src/playground/` and builds into `serve/static/`.
- `chat/` — the serve Chat view's agentic harness (a third adapter over the same core as `mcp/` and `serve/`). `tools.py` holds the 15 tool specs + `ToolRegistry` (instantiated **once per user turn** — it carries the rationale-generation budget); `files.py` is the clamped read-only file access; `stats_sql.py` is the statistics surface — read-only, **aggregate-only** SQL behind a SQLite authorizer, with a table allowlist that denies `chat_*` and `rationale_cache` (see its module docstring for the four layers); `harness.py` is `run_turn` (the tool loop) and `build_window` (token-budgeted context trimming); `prompts/system.md` is the packaged system prompt. The harness is **persistence-free** — `serve/chat.py` owns every row.
- `services/` — external integrations: `git/`, `github/`, `codegraph/` (reads CodeGraph's SQLite by `node_id`), `llm/` (Anthropic / OpenAI / OpenRouter / DeepSeek / Ollama / claude-cli). Two **parallel ports** live here: `client.py`'s `LlmClient` (sync `complete()`, used by analyze/rationale) and `chat.py`'s `ChatClient` (streaming + tool-calling, used by `chat/`). They are deliberately separate — see `chat.py`'s docstring.
- `scan/` — crawler orchestration. `crawler.py` drives `git_crawler.py`, `github_crawler.py`, and `analyze_crawler.py` per-source phases.
- `analyze/` — LLM-backed analysis. `description.py` / `llm_descriptor.py` produce per-commit diff descriptions; `rationale.py` / `rationale_generator.py` produce the 5-section rationale cards; `backfill.py` runs the lazy on-read backfill. Prompt templates live under `analyze/prompts/`.
- `agents.py` — registry of supported LLM agents (Claude Code, Cursor, VS Code / Copilot, Codex, Claude Desktop) and the per-agent MCP config wiring (`write_snippet` / `render_snippet`). `whygraph init --agent X` reads from here.
- `assets.py` + `assets/claude-code/` — bundled Claude Code assets (agents, commands, skills) copied into a project's `.claude/` by `whygraph init --agent claude`. Loaded at runtime via `importlib.resources.files("whygraph") / "assets" / "claude-code"`; same packaging precedent as `analyze/prompts/`.
- `hooks.py` — the auto-rescan git hooks (`post-commit` / `post-merge` / `post-rewrite` / `post-checkout`): the helper script, the sentinel-guarded dispatcher, and `sync_hooks()`. A top-level module for the same reason as `agents.py` / `assets.py` — an installed-by-`init` concern — and deliberately Click-free (it raises `HooksError`, not `ClickException`).
- `__main__.py` — enables `python -m whygraph`.

Console scripts in `pyproject.toml`: `whygraph` → `cli:main`, `whygraph-mcp` → `mcp.server:main`. Both must keep working — `.mcp.json` files written by `whygraph init` and the `uv tool install` path depend on them.

## Install path

WhyGraph installs **per project**, per agent:

1. `uv tool install whygraph` (or `pipx install whygraph`) so `whygraph` and `whygraph-mcp` are on `PATH`.
2. From the target repo: `whygraph init --agent <name>` — `--agent claude` writes `.mcp.json` and copies the bundled assets into `.claude/`; other agents (cursor / vscode / codex / claude-desktop) just wire their MCP config. Every agent's asset install also drops a **CodeGraph usage-guidance block** into that agent's always-on instructions (`.claude/CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` via append-merge; an `alwaysApply: true` rule for Cursor) so the agent prefers the `codegraph_*` tools over grep without the user running CodeGraph's own installer.

There is no Claude Code marketplace install; `whygraph init --agent claude` is the only path. The bundled assets are version-controlled in this repo under `src/whygraph/assets/claude-code/` — that is the source of truth, the wheel ships them, and a re-run of `whygraph init` brings a project's `.claude/` up to date (use `--force` to overwrite local edits).

## Docker delivery (the default install)

WhyGraph ships as a self-contained image so a developer needs **only Docker** on the host — no Python / Node / gh / codegraph install. The image is the only thing that gets installed; the **front door is a tag-pinned `curl … | sh`** (`scripts/install.sh` fetched from `raw.githubusercontent.com`). The whole UX is three steps:

```bash
curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v1.1.0/scripts/install.sh | sh
whygraph init     # in a repo
whygraph scan
```

**Two components, one generator.** `scripts/install.sh` is a **host-side bootstrapper** — it probes `docker` + the daemon, pulls the pinned image, then delegates to `docker run --rm <image> whygraph install` and executes that output only after checking it is **non-empty**. It carries **no shim bodies**; the in-image **`whygraph install` command** (`cli/commands/install.py`) is the single generator, writing the `whygraph` (and `whygraph-mcp`) **shims** onto PATH, each wrapping `docker run --rm -v "$PWD:/workspace" -w /workspace <image> whygraph "$@"`. That split is load-bearing: shim bodies stay in tested Python (`tests/test_install_cmd.py`) and the shell script stays thin enough that a frozen per-tag copy can't be wrong about WhyGraph. `tests/test_install_script.py` asserts the absence of shim bodies, the `main "$@"` truncation-safe shape, and that **every** failure path exits non-zero — the old `docker run … install | sh` front door exited **0** when it installed nothing, because docker's error went to stderr and `sh` read an empty stdin.

The `docker run … whygraph install | sh` form remains supported (no-curl / air-gapped / CI) and, without the pipe, is the way to inspect what would be written. The bare-`install` **launcher** (`/usr/local/bin/install`) was **removed** — call the command explicitly. There is deliberately **no `ENTRYPOINT`** so `docker run <image> codegraph …` and `<image> whygraph-mcp` still resolve. The shims bake `ghcr.io/mtrdesign/whygraph:<version>` from the image's `WHYGRAPH_VERSION` (baked at build), so an install **pins the concrete release** even via `:latest`; each shim still honours a `WHYGRAPH_IMAGE` override at run time. The container is **ephemeral per command** — no compose, no `docker exec`, no long-running container. The image is built from `docker/whygraph/Dockerfile` (base `python:3.12-slim` + git + gh + Node 22 + pinned CodeGraph CLI + WhyGraph) and published by `.github/workflows/cd-deploy-whygraph.yml` (which passes `WHYGRAPH_VERSION` + a pinned `CODEGRAPH_VERSION` as build-args).

**The version is written once, in the URL** — the tag names it and `DEFAULT_VERSION` in `scripts/install.sh` matches, so `-s <ver>` / `WHYGRAPH_VERSION` are overrides, not requirements. That makes a **per-release ritual**: bump `DEFAULT_VERSION` *and* the README URL to the release being cut. Two gates enforce it, because discipline alone demonstrably fails here (`pyproject.toml`'s `version` has never been bumped) — `tests/test_install_script.py` checks script-vs-README agreement on every PR, and a step in `cd-deploy-whygraph.yml` checks both against the release tag **before** the image is pushed. A forgotten bump is a red release, not a wrong pin. A per-tag script is frozen, so `…/main/scripts/install.sh` is the documented valve for an installer bug.

Invariants that keep the shim correct — preserve them:

- **Each command is a fresh process against `cwd`.** `get_config()` is globally memoized and config / DB-path discovery walks up from `cwd` to the `.git` root (`core/__init__.py`, `db/engine.py`); the shim mounts the repo at `/workspace` and runs there, so each `whygraph` picks up that repo's own `whygraph.toml`, `.whygraph/`, `.codegraph/`. Ephemeral `docker run` makes this automatic — don't reintroduce an in-process loop that scans multiple repos without resetting config.
- **Host-user file ownership.** The shim runs `--user "$(id -u):$(id -g)" -e HOME=/tmp` so `.whygraph/` and `.codegraph/` come back owned by the host user and git sees matching ownership (no "dubious ownership"). Keep these when editing the shim.
- **GitHub token is per-project.** `[scan].token` in each repo's `whygraph.toml` (gitignored, never committed) is exported as `GH_TOKEN` for that scan's `gh` calls (`cli/commands/scan.py:_apply_github_token`), falling back to ambient `GH_TOKEN` / `GITHUB_TOKEN` (the shim passes those through). Do **not** bake a token into the image or assume a container-wide token.

CodeGraph indexing belongs to **`whygraph scan`, not `whygraph init`** — `init` only bootstraps the WhyGraph DB / config / agent wiring (and its preflight no longer requires `docker`). `whygraph scan` builds or refreshes the index each run — `codegraph init -i` on first run, `codegraph sync -q` thereafter — gated by `--codegraph/--no-codegraph`. CodeGraph runs from the **in-image `codegraph` binary** (no docker-in-docker, no host socket): `bootstrap.py` (`services/codegraph/`) prefers the local binary and only falls back to `docker run` on native (`uv`/`pipx`) hosts without it. There is a **single image** — `ghcr.io/mtrdesign/whygraph` — and that fallback runs `codegraph` inside it (`docker run … whygraph codegraph …`); there is no separate codegraph image.

### Auto-rescan git hooks

`whygraph init` installs them (`hooks.py`, a top-level module beside `agents.py` / `assets.py` — there is **no** `hooks` CLI command — the group was removed). Four hooks — `post-commit` / `post-merge` / `post-rewrite` / `post-checkout` — keep the DBs current as the developer works, no daemon. Each execs a shared helper (`.whygraph/hooks/whygraph-scan`, gitignored) that runs `whygraph scan --skip-analyze --no-remote` (git history + `codegraph sync` only — fast, offline, no token; LLM descriptions stay on lazy backfill). The helper is **detached** (commits return instantly) and **single-flight + coalescing** (portable `mkdir` lock + a `pending` flag, since macOS has no `flock`), so rapid commits neither stack nor drop the latest `HEAD`. Installs are **sentinel-guarded** (`# >>> whygraph managed >>>`) and append to a foreign hook rather than clobber it. The dispatcher forwards `"$@"` because `post-checkout` is the only hook git invokes with arguments; the helper's arg gate skips a file checkout and a same-commit `git switch -c`. The `--no-remote` flag on `scan` exists for this path; `db/engine.py` sets `PRAGMA busy_timeout` so a background rescan and a manual scan don't collide.

`[scan].hooks` is the only switch — a bool *or* a list of hook names — and **`init` is the reconciler**: `sync_hooks()` iterates all of `HOOK_NAMES` every run, installing what is listed and stripping the managed block from what is not, so shrinking the list removes the dropped hooks. It is one function rather than an install/uninstall pair precisely so the removal half cannot be forgotten. It is best-effort: an unwritable hooks dir or an unknown hook name warns and `init` still exits 0.

### Branch membership

`on_default_branch` is **computed, not assumed**. `Repository.default_branch_refs` resolves the default branch (`origin/HEAD` → `<remote>/main` → `<remote>/master`, overridable via `[scan].default_branch`) and unions it with the same-named *local* branch, so unpushed commits on local `main` still count. `GitCrawler` flags new rows against that SHA set and records `first_seen_ref` (a branch name, or `refs/pull/<N>/head` for a `PROriginEnricher` recovery; `NULL` means it was on the default branch) — and a **reconcile pass** recomputes the flag for existing rows on every scan, so the DB self-heals as branches merge or get rewritten. Two guards make a mass-demotion impossible: an unresolvable default branch and a shallow clone both skip the pass entirely. Rows are never deleted — an unreachable commit is still evidence. `first_seen_ref` has exactly one consumer beyond debugging: the rename-alias walks in `mcp/path_history.py` / `mcp/evidence.py` scope to *default branch **or** current branch*, which keeps an in-flight rename visible without letting an abandoned branch pollute path history forever.

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
