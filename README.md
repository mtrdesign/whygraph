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
ANTHROPIC_API_KEY=… npm run whygraph rationale <node|qname>  # collect evidence + generate brief on demand
npm run whygraph evidence <node|qname>        # inspect (or auto-collect) raw evidence
npm run whygraph ingest                       # OPTIONAL batch warm-up over every node
```

`init` creates `.whygraph/whygraph.db` in the current directory. `codegraph-stats` walks up from `cwd` to find a `.codegraph/codegraph.db` (override with `CODEGRAPH_DB`).

**Lazy by default.** `rationale` and `evidence` collect upstream signals (git blame + commits, plus GitHub PRs/issues if available) on first request for a symbol and cache them. Subsequent requests reuse the cache for `WHYGRAPH_EVIDENCE_TTL_DAYS` days (default 14) unless the file has new commits since collection — in which case the cache is refreshed automatically. Pass `--refresh-evidence` (rationale) or `--refresh` (evidence) to force re-collection. `--force` (rationale only) bypasses the rationale cache so Claude regenerates against fresh evidence.

`rationale` calls Claude (default `claude-sonnet-4-6`, override with `WHYGRAPH_MODEL`) and caches by `(bundle_hash, prompt_version, model)`. `ingest` is now optional — useful for batch warm-up before a demo or in CI. Use `--no-github` to skip the GitHub side, `--refresh` to recollect everything.

### Two backends for rationale generation

`WHYGRAPH_RATIONALE_BACKEND` selects how Claude is called:

- `api` (default) — direct Anthropic SDK call billed against `ANTHROPIC_API_KEY`'s API credit balance.
- `claude_cli` — shells out to `claude -p` (the Claude Code CLI) with `ANTHROPIC_API_KEY` stripped from the subprocess env, so it falls back to Claude Code's OAuth and bills against your **Claude Pro/Max subscription tokens** instead of API credits. Requires `claude` to be installed and signed in.

Use `claude_cli` when you have a Claude Code subscription but no API credits on the same workspace.

GitHub collection requires the [`gh`](https://cli.github.com/) CLI authenticated against your account, and a `github.com` `origin` remote. Anything else auto-skips silently per symbol.

## MCP integration (Claude Code)

`whygraph mcp` runs a stdio MCP server exposing two tools:

- `whygraph_rationale_pre_edit_brief({target, force?, response_format?})` — cached or freshly-generated rationale for a symbol (calls Claude on cache miss; needs `ANTHROPIC_API_KEY`).
- `whygraph_evidence_for({target, response_format?})` — raw evidence rows for a symbol (read-only).

`target` is a CodeGraph node ID or qualified_name.

### 1. Install the skill

Copy `examples/skills/whygraph-pre-edit/` into your project's `.claude/skills/` directory:

```bash
cp -R /path/to/whygraph/examples/skills/whygraph-pre-edit ./.claude/skills/
```

This tells Claude Code *when* to call the brief tool — before edits, refactors, deletions, and "why does this exist?" questions.

### 2. (Optional) Install the `/rationale` slash command

Copy the example command into your project for an explicit lookup shortcut:

```bash
mkdir -p .claude/commands
cp /path/to/whygraph/examples/commands/rationale.md .claude/commands/
```

Then in Claude Code, type:

```
/rationale Page              # markdown brief
/rationale Page --json       # raw JSON payload
/rationale Page --refresh    # recollect upstream evidence first
/rationale Page --force      # bypass rationale cache, regenerate
```

The command calls the MCP tool and prints the result verbatim. Read-only — never edits files.

### 3. Wire up the MCP server

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
