# whygraph

Rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph): explains *why* code exists, not just what it does.

For each symbol, WhyGraph collects evidence from git history, GitHub, tests, and code comments, then generates a rationale (purpose, constraints, tradeoffs, risks) with a deterministic confidence score. Exposed to Claude Code via MCP so AI assistants can read the *intent* behind code before editing it.

**Status:** early — v0 in progress.

## Quick start (dev)

Requires Node 18+ (`.nvmrc` pins to 22).

```bash
nvm use         # picks up .nvmrc
npm install
npm run whygraph init
npm run whygraph codegraph-stats              # needs a .codegraph/codegraph.db nearby
npm run whygraph ingest                       # collect git evidence for every node
npm run whygraph evidence <node|qname>        # inspect stored evidence
ANTHROPIC_API_KEY=… npm run whygraph rationale <node|qname>  # generate or fetch cached rationale
```

`init` creates `.whygraph/whygraph.db` in the current directory. `codegraph-stats` walks up from `cwd` to find a `.codegraph/codegraph.db` (override with `CODEGRAPH_DB`). `ingest` writes git evidence for every CodeGraph node into the WhyGraph DB. `rationale` calls Claude (default `claude-sonnet-4-6`, override with `WHYGRAPH_MODEL`) and caches by `(bundle_hash, prompt_version, model)` — pass `--force` to regenerate.

## MCP integration (Claude Code)

`whygraph mcp` runs a stdio MCP server exposing two tools:

- `whygraph_rationale_pre_edit_brief({target, force?, response_format?})` — cached or freshly-generated rationale for a symbol (calls Claude on cache miss; needs `ANTHROPIC_API_KEY`).
- `whygraph_evidence_for({target, response_format?})` — raw evidence rows for a symbol (read-only).

`target` is a CodeGraph node ID or qualified_name. Wire it into Claude Code's MCP config:

```json
{
  "mcpServers": {
    "whygraph": {
      "command": "npx",
      "args": ["tsx", "/absolute/path/to/whygraph/src/index.ts", "mcp"],
      "env": {
        "CODEGRAPH_DB": "/absolute/path/to/your/project/.codegraph/codegraph.db",
        "WHYGRAPH_DB": "/absolute/path/to/your/project/.whygraph/whygraph.db",
        "ANTHROPIC_API_KEY": "..."
      }
    }
  }
}
```

## Layout

- `src/db/` — SQLite schema and client
- `src/config.ts` — paths and env (`WHYGRAPH_DB`, `CODEGRAPH_DB`, `ANTHROPIC_API_KEY`, `WHYGRAPH_MODEL`)
- `src/index.ts` — CLI entry

More modules (CodeGraph reader, evidence collectors, rationale generator, MCP server) land as they're built.
