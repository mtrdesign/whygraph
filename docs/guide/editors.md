# Wiring your editor

`whygraph-mcp` is a standalone MCP server, so any agent that speaks MCP can use it.
`whygraph init --agent X` writes the right config to the right place for each one.

Run it from the repo you want WhyGraph to analyze:

```bash
whygraph init --agent claude
```

## Supported agents

Four agents are supported. **All of them are project-scoped** - the config file is written or merged
inside the repo, so you can commit it and every teammate's editor picks it up.

| `--agent` | Editor | Config file | Assets land in |
|---|---|---|---|
| `claude` | Claude Code | `.mcp.json` (repo root) | `.claude/` |
| `cursor` | Cursor | `.cursor/mcp.json` | `.cursor/` |
| `vscode` (alias `copilot`) | VS Code / GitHub Copilot | `.vscode/mcp.json` | `.github/` |
| `codex` | OpenAI Codex | `.codex/config.toml` | repo root + `.codex/agents/` |

Agent names are case-insensitive. Run `whygraph init --help` for the list with each one's format and
scope.

The generated config launches `whygraph-mcp` by bare command name, so the same checked-in file works
for everyone who has WhyGraph installed - no absolute paths to scrub.

## Bundled assets

**Every agent gets an asset tree**, not just Claude Code - subagents, commands, and skills that teach
your editor how to use WhyGraph's tools. Re-running leaves your existing files alone; pass `--force`
to overwrite them.

```bash
whygraph init --agent cursor           # wire MCP + copy the .cursor/ assets
whygraph init --agent cursor --force   # overwrite existing asset files
```

Each agent's install also **append-merges** a CodeGraph usage-guidance block into that agent's
always-on instructions - `CLAUDE.md`, `AGENTS.md`, or `.github/copilot-instructions.md`, and an
always-apply rule for Cursor. Your own content is preserved; the block is added below it.

## What else `init` does

Wiring an editor is one step of `whygraph init`, not the whole of it. The same run also:

- Runs preflight diagnostics.
- Prompts interactively for your LLM providers, source-control provider, and git hooks (unless
  `--yes`, or stdin isn't a TTY).
- Writes `whygraph.example.toml` and updates `.gitignore`.
- **Reconciles the auto-rescan git hooks** in `.git/hooks` against `[scan].hooks` - see
  [Keep it fresh](scanning.md#keep-it-fresh).

| Flag | What it does |
|---|---|
| `--force` | Overwrite existing asset files in the destination directory. |
| `--yes` / `-y` | Accept all defaults without prompting. |

## Verify

After wiring, confirm the server launches:

```bash
whygraph-mcp   # Ctrl-C to exit
```

If it starts cleanly, your editor can start it too. Next, see how an agent
[actually calls the tools](mcp-usage.md).
