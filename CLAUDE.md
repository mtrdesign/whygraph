# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

This is the `1.x` branch — a Python rewrite in progress. Today the package is a **scaffold only**: a `whygraph` CLI with one `version` subcommand and an empty FastMCP stdio server (`whygraph-mcp`) that registers no tools. Features land incrementally on this branch. The merged TypeScript POC lives on [`main`](https://github.com/cvetty/whygraph/tree/main); commit history before `85fe8b3` ("initial codebase") describes the v0 design (CodeGraph reader, evidence collectors, rationale generator, MCP tools, slash command, install command) and is the reference for what v1 should re-implement in Python.

`v1-plan.md` is the authoritative design note. Decisions already captured there — read it before adding architecture:

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

If `uv` fails with `UnknownIssuer` SSL errors, prefix with `SSL_CERT_FILE= ` (the user's `SSL_CERT_FILE` env var points at a corp-only bundle that breaks public TLS off-VPN).

## Architecture (current scaffold)

- `src/whygraph/cli.py` — Click group exposing the `whygraph` command. Subcommands attach to `main`.
- `src/whygraph/mcp_server.py` — `FastMCP("whygraph")` instance plus a `main()` that runs `transport="stdio"`. New MCP tools register on the module-level `mcp` object via `@mcp.tool()`.
- `src/whygraph/__main__.py` — enables `python -m whygraph`.
- Console scripts in `pyproject.toml`: `whygraph` → `cli:main`, `whygraph-mcp` → `mcp_server:main`. Both must keep working — the plugin's `.mcp.json` and `uv tool install` paths depend on them.

## Plugin & marketplace layout

This repo doubles as a single-plugin Claude Code marketplace.

- `.claude-plugin/marketplace.json` — marketplace manifest, points at `./plugins/whygraph`.
- `plugins/whygraph/.claude-plugin/plugin.json` — plugin manifest.
- `plugins/whygraph/.mcp.json` — launches the MCP server with `uv run --project ${CLAUDE_PLUGIN_ROOT}/../.. whygraph-mcp`. The `${CLAUDE_PLUGIN_ROOT}/../..` path resolves to the repo root, so changing the plugin's directory depth requires updating this. Avoid replacing `uv` with bare `python` — the dev workflow assumes uv-managed venvs.

Install locally: `/plugin marketplace add /absolute/path/to/whygraph` then `/plugin install whygraph@whygraph`.

## Conventions

- **Don't add new top-level dirs** without updating `[tool.hatch.build.targets.wheel].packages` in `pyproject.toml` (currently `["src/whygraph"]`).
- **Tests live in `tests/`** (configured via `[tool.pytest.ini_options].testpaths`). `test_smoke.py` asserts the package imports and the MCP server is named `"whygraph"` — preserve both invariants when restructuring.
- **Companion repo:** CodeGraph upstream is `colbymchenry/codegraph`. WhyGraph reads its SQLite output and joins by `node_id`. Schema reference: tables `nodes`, `edges`, `files`, `nodes_fts`, `unresolved_refs`.

## Working principles

Adapted from [andrej-karpathy-skills/EXAMPLES.md](https://github.com/forrestchang/andrej-karpathy-skills/blob/main/EXAMPLES.md). These guide how to approach work in this repo.

### 1. Think before coding

Surface assumptions before writing code. If a request has multiple plausible interpretations, name them and ask — don't pick silently.

- "Add caching for rationale cards" — in-memory per-process? On-disk under `~/.cache/whygraph`? Keyed by `node_id` or by `qualified_name + file_path`? `v1-plan.md` already pins the cache key to be content-addressable, but TTL, location, and invalidation are open. Ask before implementing.
- "Make graph queries faster" — lower latency on a single `get_callers`, higher throughput across many calls, or perceived speed via streaming partial results? Each implies a different change.

### 2. Simplicity first

Solve today's problem with the smallest thing that works. Add abstraction when a second concrete case forces it, not in anticipation.

- For a one-off SQLite read, a function in `sqlite_codegraph_backend.py` is enough. Don't introduce a `QueryBuilder` class until a second backend actually needs to share query logic.
- The `GraphBackend` protocol in `v1-plan.md` is the *exception that proves the rule*: it's introduced up-front because three concrete backends are already named (`SqliteCodegraphBackend`, `JsonGraphifyBackend`, `MCPBackend`). Without that, a single backend wouldn't justify a protocol.

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
