# whygraph

Rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph): explains *why* code exists, not just what it does.

For each symbol, WhyGraph collects evidence from git history, GitHub, tests, and code comments, then generates a rationale (purpose, constraints, tradeoffs, risks) with a deterministic confidence score. Exposed to Claude Code via MCP so AI assistants can read the *intent* behind code before editing it.

> **Status:** v1.x rewrite in progress — both MCP tools (`whygraph_evidence_for` and `whygraph_rationale_pre_edit_brief`) are functional, backed by a `GraphBackend` abstraction with `SqliteCodegraphBackend` as the first implementation. Slash command (`/whygraph-plan`) and planner subagent are still ahead. The TS POC lives on [`main`](https://github.com/cvetty/whygraph/tree/main).

## Layout

```
.
├── .claude-plugin/marketplace.json       # single-plugin marketplace
├── plugins/whygraph/                     # the Claude Code plugin
│   ├── .claude-plugin/plugin.json        # plugin manifest
│   └── .mcp.json                         # MCP server launch config
├── src/whygraph/                         # Python package
│   ├── cli.py                            # `whygraph` CLI
│   ├── mcp_server.py                     # FastMCP stdio server (tools below)
│   ├── config.py                         # env-injected Config + DB path discovery
│   ├── db.py                             # WhyGraph SQLite (evidence + rationale)
│   ├── backend.py                        # GraphBackend Protocol + SqliteCodegraphBackend
│   ├── evidence.py                       # git + GitHub collectors, cache w/ HEAD-sha staleness
│   ├── prompts.py                        # Pydantic Rationale schema + prompt v3
│   └── rationale.py                      # LLM clients (CLI default, SDK opt-in) + cache
├── tests/
└── pyproject.toml                        # uv-managed
```

## Tools exposed

The MCP server registers two tools:

- **`whygraph_evidence_for`** — returns raw evidence rows (git commits, blame, optional GitHub PRs/issues) for a code symbol. Cached per project; recollects when the file's HEAD sha advances or after the TTL. Never calls Claude.
- **`whygraph_rationale_pre_edit_brief`** — returns the rationale (purpose, why, constraints, tradeoffs, risks) for a code symbol *before* editing it. Lazily collects evidence on first request and caches the generated rationale; subsequent requests reuse the cache when `(bundle_hash, prompt_version, model)` matches.

Both accept `target` (CodeGraph node ID or `qualified_name`) and `response_format` (`markdown` default, or `json`). The rationale tool also takes `force` (bypass rationale cache) and `refresh_evidence` (recollect evidence).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CODEGRAPH_DB` | walk-up search for `.codegraph/codegraph.db` | Path to the CodeGraph SQLite DB. |
| `WHYGRAPH_DB` | walk-up search → `<repo>/.whygraph/whygraph.db` | Where WhyGraph stores its evidence + rationale cache. |
| `WHYGRAPH_MODEL` | `claude-sonnet-4-6` | Model used when generating rationale. |
| `WHYGRAPH_RATIONALE_BACKEND` | `claude_cli` (or `api` if `ANTHROPIC_API_KEY` is set) | `claude_cli` spawns the local `claude` CLI (uses your Pro/Max plan via OAuth). `api` calls the Anthropic API directly. |
| `WHYGRAPH_EVIDENCE_TTL_DAYS` | `14` | How long an evidence bundle stays fresh before recollection. |
| `ANTHROPIC_API_KEY` | unset | Required iff backend is `api`. Stripped from the child env when backend is `claude_cli` so the CLI falls back to OAuth instead of direct-API billing. |

The `claude_cli` backend is the default because it routes inference through your existing Claude Code session (no separate API billing). MCP sampling — the architecturally correct path for this — is not yet supported by Claude Code (issue [#1785](https://github.com/anthropics/claude-code/issues/1785)). When it lands, an `McpSamplingClient` will slot in cleanly behind the existing `LLMClient` Protocol.

## Develop

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11 (uv installs the pinned version automatically).

```bash
uv sync                  # bootstrap .venv and install deps
uv run pytest            # smoke tests
uv run whygraph version  # CLI sanity check
uv run whygraph-mcp      # launch the MCP server on stdio (Ctrl-C to exit)
```

### Debug the MCP server with MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the official web UI for poking at a stdio MCP server — list tools, call them with custom args, see raw responses, and tail stderr.

```bash
npx @modelcontextprotocol/inspector uv run whygraph-mcp
```

It prints a `http://localhost:…` URL with a one-time auth token; open it. The Inspector spawns `whygraph-mcp` as a subprocess and connects via stdio. Use **Reconnect** to pick up code changes.

## Install as a Claude Code plugin

From any project where you want WhyGraph available:

```
/plugin marketplace add /absolute/path/to/whygraph
/plugin install whygraph@whygraph
```

(Once published, replace the local path with `cvetty/whygraph`.)

After install, the `whygraph` MCP server is launched on demand by Claude Code via `uv run --project <plugin-checkout> whygraph-mcp`. Verify it loaded with `/mcp`.

## Install the standalone CLI

```bash
uv tool install /absolute/path/to/whygraph
whygraph version
```

This puts `whygraph` and `whygraph-mcp` on your `PATH`, independent of the plugin.
