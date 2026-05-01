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

- `whygraph_rationale_pre_edit_brief({target, force?, response_format?})` — cached or freshly-generated rationale for a symbol (calls Claude on cache miss).
- `whygraph_evidence_for({target, response_format?})` — raw evidence rows for a symbol (read-only).

`target` is a CodeGraph node ID or qualified_name.

### Install once, use everywhere (recommended)

From your whygraph checkout:

```bash
npm run whygraph install -- --global
# or:  npm run whygraph install -- --global --backend api --force
```

This:

- Registers a `whygraph` MCP server at user scope via `claude mcp add-json -s user` (lives in `~/.claude.json`, available in every project).
- Copies the `whygraph-pre-edit` skill into `~/.claude/skills/`.
- Copies the `/rationale` slash command into `~/.claude/commands/`.

The MCP entry has **no project-specific paths** — at runtime the server walks up from Claude Code's working directory to find `.codegraph/codegraph.db` and `.whygraph/whygraph.db`. The `.whygraph/whygraph.db` is created automatically on first call.

Per-project requirement: each project must have a `.codegraph/codegraph.db` (run CodeGraph there first). If absent, the MCP tools return a friendly error instead of failing silently.

`--force` re-registers the MCP server and overwrites the skill/command files. Backend defaults to `claude_cli` (uses your Claude Pro/Max subscription); pass `--backend api` to bill against `ANTHROPIC_API_KEY` instead — the install drops a `${ANTHROPIC_API_KEY}` placeholder so you set it in the launching shell, not in the config file.

> **Path coupling.** The user-scope MCP entry contains an absolute path to your whygraph checkout. If you move the whygraph repo, re-run install with `--force`.

### Install into a single project

If you'd rather scope WhyGraph to one repo (project-level `.mcp.json`, project-local DB), drop `--global`:

```bash
npm run whygraph install -- --dir /path/to/your/project
```

This:

- Verifies a CodeGraph DB exists at `<target>/.codegraph/codegraph.db`.
- Creates `<target>/.whygraph/whygraph.db` (with a local `.gitignore` so the DB never gets committed).
- Copies the skill into `<target>/.claude/skills/` and the slash command into `<target>/.claude/commands/`.
- Writes (or merges) `<target>/.mcp.json` with a `whygraph` server entry pointing at this checkout's `src/index.ts`.

`--force` overwrites the `whygraph` entry, slash command, and skill files; the WhyGraph DB is always preserved. Project-level entries override user-level ones, so a project-scope install always takes precedence over `--global`.

### Using the install

After either install, restart Claude Code in the target project. Then:

```
/rationale Page              # markdown brief
/rationale Page --json       # raw JSON payload
/rationale Page --refresh    # recollect upstream evidence first
/rationale Page --force      # bypass rationale cache, regenerate
```

The skill also tells Claude Code to call `whygraph_rationale_pre_edit_brief` automatically before edits, refactors, deletions, and "why does this exist?" questions.

> The MCP config writes absolute paths (your whygraph checkout, the project's CodeGraph/WhyGraph DBs). If you move the whygraph checkout or the project, re-run install with `--force`. `.mcp.json` is project-local — review it before committing.

### Manual setup (alternative)

If you'd rather wire it up by hand, drop this into `<target>/.mcp.json`:

```json
{
  "mcpServers": {
    "whygraph": {
      "command": "npx",
      "args": ["tsx", "/absolute/path/to/whygraph/src/index.ts", "mcp"],
      "env": {
        "CODEGRAPH_DB": "/absolute/path/to/your/project/.codegraph/codegraph.db",
        "WHYGRAPH_DB": "/absolute/path/to/your/project/.whygraph/whygraph.db",
        "WHYGRAPH_RATIONALE_BACKEND": "claude_cli"
      }
    }
  }
}
```

Copy the skill (`examples/skills/whygraph-pre-edit/`) and slash command (`examples/commands/rationale.md`) into `<target>/.claude/skills/` and `<target>/.claude/commands/` respectively.

## Layout

- `src/db/` — SQLite schema and client
- `src/config.ts` — paths and env (`WHYGRAPH_DB`, `CODEGRAPH_DB`, `ANTHROPIC_API_KEY`, `WHYGRAPH_MODEL`)
- `src/index.ts` — CLI entry

More modules (CodeGraph reader, evidence collectors, rationale generator, MCP server) land as they're built.
