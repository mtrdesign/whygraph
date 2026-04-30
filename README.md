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
npm run whygraph codegraph-stats   # needs a .codegraph/codegraph.db nearby
```

`init` creates `.whygraph/whygraph.db` in the current directory. `codegraph-stats` walks up from `cwd` to find a `.codegraph/codegraph.db` (override with `CODEGRAPH_DB`) and prints summary counts.

## Layout

- `src/db/` — SQLite schema and client
- `src/config.ts` — paths and env (`WHYGRAPH_DB`, `CODEGRAPH_DB`, `ANTHROPIC_API_KEY`, `WHYGRAPH_MODEL`)
- `src/index.ts` — CLI entry

More modules (CodeGraph reader, evidence collectors, rationale generator, MCP server) land as they're built.
