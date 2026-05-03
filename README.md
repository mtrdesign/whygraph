# whygraph

Rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph): explains *why* code exists, not just what it does.

For each symbol, WhyGraph collects evidence from git history, GitHub, tests, and code comments, then generates a rationale (purpose, constraints, tradeoffs, risks) with a deterministic confidence score. Exposed to Claude Code via MCP so AI assistants can read the *intent* behind code before editing it.

> **Status:** v1.x rewrite in progress — currently just the scaffold (empty MCP server, no tools yet). Feature implementation lands incrementally on this branch. The TS POC lives on [`main`](https://github.com/cvetty/whygraph/tree/main).

## Layout

```
.
├── .claude-plugin/marketplace.json       # single-plugin marketplace
├── plugins/whygraph/                     # the Claude Code plugin
│   ├── .claude-plugin/plugin.json        # plugin manifest
│   └── .mcp.json                         # MCP server launch config
├── src/whygraph/                         # Python package
│   ├── cli.py                            # `whygraph` CLI
│   └── mcp_server.py                     # FastMCP stdio server
├── tests/
└── pyproject.toml                        # uv-managed
```

## Develop

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11 (uv installs the pinned version automatically).

```bash
uv sync                  # bootstrap .venv and install deps
uv run pytest            # smoke tests
uv run whygraph version  # CLI sanity check
uv run whygraph-mcp      # launch the MCP server on stdio (Ctrl-C to exit)
```

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
