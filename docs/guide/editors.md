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

Run `whygraph init --list-agents` to print these paths for your own checkout.

The generated config launches `whygraph-mcp` by bare command name, so the same checked-in file works
for everyone who has WhyGraph installed - no absolute paths to scrub.

## Claude Code assets

`--agent claude` does one extra thing: it copies a bundled asset tree into `.claude/`. Re-running
leaves your existing files alone; pass `--force` to overwrite them.

```bash
whygraph init --agent claude --no-install-assets   # MCP wiring only, skip the .claude/ copy
whygraph init --agent claude --force               # overwrite existing .claude/ files
```

## Useful flags

| Flag | What it does |
|---|---|
| `--print` | Print the MCP snippet to stdout instead of writing any file. Good for pasting by hand. |
| `--list-agents` | List supported agents and their config paths, then exit. |
| `--install-assets / --no-install-assets` | Copy (or skip) the agent's bundled assets. Default: copy. No-op for agents with no asset tree. |
| `--skip-preflight` | Skip the host-tool diagnostics. For known-good scripted environments. |
| `--force` | Overwrite existing asset files in the destination directory. |

## Verify

After wiring, confirm the server launches:

```bash
whygraph-mcp   # Ctrl-C to exit
```

If it starts cleanly, your editor can start it too. Next, see how an agent
[actually calls the tools](mcp-usage.md).
