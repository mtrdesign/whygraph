# Connecting agents

Each project has its own MCP endpoint on the portal, `http://127.0.0.1:<port>/mcp/<slug>`. When you
[set up a project](projects.md#set-up) and select agents, the portal writes a config entry
named `whygraph` into each agent's project file, pointing at that endpoint over HTTP. Your agent then
has the same [tools, resources and prompts](../reference/mcp.md) it always had; the server name is
unchanged, so `mcp__whygraph__*` tool names still work.

The portal must be running for an agent to reach it - there is no per-session process to launch any
more.

!!! note "Linked projects use the same files"
    A checkout [linked to a platform](platform-projects.md) gets exactly the same entry and assets as a
    local-folder project, with the platform project's slug in the URL, so teammates linked to the same
    project share one committed entry. The agent still talks to **your local portal**, never to the
    platform: the local portal answers from your working tree and the platform's history.

## What gets written

All four agents are project-scoped: the file lives in your repository, so teammates can share it.

| Agent | Config file | Entry |
|---|---|---|
| Claude Code (`claude`) | `.mcp.json` | `"whygraph": {"type": "http", "url": "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/<slug>"}` under `mcpServers` |
| Cursor (`cursor`) | `.cursor/mcp.json` | `"whygraph": {"url": "http://127.0.0.1:8765/mcp/<slug>"}` under `mcpServers` |
| VS Code / Copilot (`vscode`) | `.vscode/mcp.json` | `"whygraph": {"type": "http", "url": "http://127.0.0.1:${input:whygraph-port}/mcp/<slug>"}` under `servers`, plus an `inputs` entry that prompts for the port, default `8765` |
| Codex (`codex`) | `.codex/config.toml` | `[mcp_servers.whygraph]` with `url = "http://127.0.0.1:8765/mcp/<slug>"` |

Each agent also gets its bundled assets - skills, commands and subagents that teach it when to call
WhyGraph - copied into `.claude/`, `.cursor/`, `.github/` or the repo root and `.codex/agents/`, and a
CodeGraph usage-guidance block appended to its always-on instructions (`CLAUDE.md`, `AGENTS.md`,
`.github/copilot-instructions.md`, or an always-apply rule for Cursor). Re-initializing leaves your
edited files alone; **Update agent files** in the project's Settings overwrites them.

## Safe to commit

Every one of these files may be committed. On the default port (`8765`) the entry is identical for
every teammate, because it carries no path and no machine name:

- Claude Code and VS Code read the port from an **environment variable** or a **prompt**, with `8765` as
  the default.
- Cursor and Codex do not document a default for an unset variable, so their entries carry the port
  **literally**. An unset variable would otherwise drop the port and break the entry for everyone.

The slug in the URL comes from the repository's name, so teammates normally end up with the same one.
If another project already had that name on your portal, the slug gets a numeric suffix and the entry
is specific to your machine; the preview tells you when that happens.

If an agent file is already tracked by git, the portal shows a diff first and writes it only after you
confirm. If it cannot parse the file safely - JSONC with comments, or TOML with comments - it leaves the
file alone and shows the entry to paste yourself. Before any rewrite, the old file is copied to
`.whygraph/backups/`.

## A non-default port

If you started the portal with `--port`, the entries written at that time already carry it. When you
change the port later, the portal rewrites the markers in your repositories itself, and follows the
agent entries by what each agent supports:

| Agent | What to do after a port change |
|---|---|
| Claude Code | Export `WHYGRAPH_PORT=<port>` in the environment you start Claude Code from. The committed file needs no edit |
| VS Code | Enter the new port when the editor prompts for it |
| Cursor | Edit the port in `.cursor/mcp.json`. The portal rewrites it for you when the file is not tracked by git |
| Codex | Edit the port in `.codex/config.toml`. The portal rewrites it for you when the file is not tracked by git |

A file tracked by git is **never rewritten automatically** on a port change - committed configs
belong to the whole team. After a port change the **Projects** page shows a notice listing what the
portal changed, the exact line to change by hand in each tracked file (with a copy button), the
environment hints for Claude Code and VS Code, and any project folders it could not reach (they still
name the old port until the portal starts with the folder available). Each project's overview and
settings show that project's part. Dismissing a notice hides it until the port changes again. The
same report is in the `port_change` field of `GET /api/portal/state` and of
`GET /api/projects/<slug>`.

## Agent-specific notes

**Claude Code** asks you to approve a project-scoped MCP server the first time it sees it. Approve
`whygraph` when prompted (or with `/mcp`); nothing connects until you do.

**VS Code** also reads a workspace-root `.mcp.json`. A repository configured for both Claude Code and
VS Code therefore lists the `whygraph` server twice in VS Code. That is noisy, not broken; disable one
of the two entries.

**Codex** loads `.codex/config.toml` only for projects you have marked as trusted. Trust the project
in Codex for the server to load.

## Agent activity

The portal counts how often agents call WhyGraph, per project, so the project's **Overview** can show
whether an agent is actually connected and what it asks for. What is counted:

- every MCP **tool call**, **resource read** and **prompt** on a project's `/mcp/<slug>` endpoint,
  under the tool, resource or prompt name (for example `whygraph_evidence_for` or `whygraph_commit`);
- on a production portal, every **data call** a [connected portal](platform-projects.md) makes for one
  of its agents (evidence, rationale, area history, a commit, a pull request, an issue, the repository
  overview).

A call counts even when the answer came from the cache. Protocol chatter (listing tools, resources or
prompts), the linked-project **status check** a local portal makes, a call refused by a rate limit,
and the Explorer and Chat views are not agent activity and are not counted.

The counter stores **counts only**: the project, the UTC day, the source (MCP or a connected portal),
who made the call and through which connection, the call's name, and how many calls. Never a path, a
symbol name, an argument or anything from the answer. Counts are written to the portal database
every 30 seconds, so a crash can lose at most the last 30 seconds of counts, and they are kept 400
days. A call that made an LLM request is also in the [usage ledger](usage.md), with its cost.

## Removing or changing agents

In the project's **Settings**, the Agents section reconfigures which agents are connected. Removing a
project can also strip the `whygraph` entries the portal wrote, under the same backup and
tracked-file rules, and leaves every other server in those files alone.

## Manual entry

If you would rather not let the portal edit a file - or it refused one - the entry to paste for each agent is shown
in the **Connect your agent** panel on the project's home page.

## Troubleshooting

- **The agent says it cannot connect.** Check `whygraph status`: the portal has to be running, on the
  port in the entry.
- **`409` from the endpoint.** The project is added but not initialized yet. Finish setup in the
  portal.
- **A 1.x entry that runs `whygraph-mcp`.** That command was removed; it prints a message and exits. Add
  the repo in the portal and migrate the entry. See [Upgrading from 1.x](upgrading.md#from-1x).
