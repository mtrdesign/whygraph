# Wiring your editor

WhyGraph reaches your editor over **HTTP MCP**. Each project has two endpoints on the
[portal](../portal/index.md): `http://127.0.0.1:<port>/mcp/<slug>`, WhyGraph's own tools, and
`http://127.0.0.1:<port>/mcp/<slug>/code`, CodeGraph's graph tools served by the portal. Any agent that
speaks MCP over HTTP can use them. You don't write that config yourself: when you
[set up a project](../portal/projects.md#set-up), you pick the agents and the portal writes
each one's entry, after showing you a preview.

## Supported agents

Four agents are supported. **All of them are project-scoped** - the config file is written or merged
inside the repo, so you can commit it and every teammate's editor picks it up.

| Agent | Editor | Config file | Assets land in |
|---|---|---|---|
| `claude` | Claude Code | `.mcp.json` (repo root) | `.claude/` |
| `cursor` | Cursor | `.cursor/mcp.json` | `.cursor/` |
| `vscode` (alias `copilot`) | VS Code / GitHub Copilot | `.vscode/mcp.json` | `.github/` |
| `codex` | OpenAI Codex | `.codex/config.toml` | repo root + `.codex/agents/` |

You can select several agents for one project, and change the set later in the project's **Settings**.

## What to know

The details live on [Connecting agents](../portal/agents.md):

- the exact entry each agent gets, and why it is **safe to commit** on the default port;
- what to do after a **non-default port** (export `WHYGRAPH_PORT` for Claude Code, edit the literal port
  for Cursor and Codex, enter it in VS Code's prompt);
- Claude Code's **approval prompt** for project-scoped servers, Codex's **trusted-project** rule, and VS
  Code **listing the server twice** when `.mcp.json` is also written;
- migrating a 1.x entry that still runs `whygraph-mcp`, which no longer exists.

## Bundled assets

**Every agent gets an asset tree**, not just Claude Code - subagents, commands, and skills that teach
your editor how to use WhyGraph's tools. Re-initializing leaves your existing files alone; **Update
agent files** in the project's Settings overwrites them.

Cursor's slash commands (`/rationale`, `/whygraph-plan`, `/whygraph-implement`, `/whygraph-note`) are installed as skills under
`.cursor/skills/`. Cursor also loads `.claude/skills/`, so with both Claude Code and Cursor selected each
command shows up twice in Cursor's slash menu. A project initialized by an earlier build has old
`.cursor/commands/` files: **Update agent files** removes them (it asks first if you edited one). Copilot also reads
`AGENTS.md`, so with both VS Code and Codex selected the two always-on blocks overlap.

Each agent's install also **append-merges** a CodeGraph usage-guidance block, pointing at the portal's
`codegraph_*` tools, into that agent's always-on instructions - `CLAUDE.md`, `AGENTS.md`, or `.github/copilot-instructions.md`, and an
always-apply rule for Cursor. Your own content is preserved; the block is added below it.

## Verify

Check the portal is running (`whygraph status`), then ask your agent to list its MCP servers - `whygraph`
and `codegraph` should both be there. Next, see how an agent [actually calls the tools](mcp-usage.md).
