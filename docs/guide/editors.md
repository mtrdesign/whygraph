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

| `--agent` | Editor | Config file |
|---|---|---|
| `claude` | Claude Code | `.mcp.json` (repo root) |
| `cursor` | Cursor | `.cursor/mcp.json` |
| `vscode` (alias `copilot`) | VS Code / GitHub Copilot | `.vscode/mcp.json` |
| `codex` | OpenAI Codex | `.codex/config.toml` |

Run `whygraph init --help` to see the supported agents.

The generated config launches `whygraph-mcp` by bare command name, so the same checked-in file works
for everyone who has WhyGraph installed - no absolute paths to scrub.

## Claude Code assets

`--agent claude` does one extra thing: it copies a bundled asset tree into `.claude/`. Re-running
leaves your existing files alone; pass `--force` to overwrite them.

```bash
whygraph init --agent claude           # wire MCP + copy the .claude/ assets
whygraph init --agent claude --force   # overwrite existing .claude/ files
```

## Useful flags

| Flag | What it does |
|---|---|
| `--force` | Overwrite existing asset files in the destination directory. |
| `--yes` / `-y` | Accept all defaults without prompting (also implied off a TTY). |

## Verify

After wiring, confirm the server launches:

```bash
whygraph-mcp   # Ctrl-C to exit
```

If it starts cleanly, your editor can start it too. Next, see how an agent
[actually calls the tools](mcp-usage.md).
