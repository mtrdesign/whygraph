# Connecting agents

Each project has two MCP endpoints on the portal: `http://127.0.0.1:<port>/mcp/<slug>`, WhyGraph's own
server, and `http://127.0.0.1:<port>/mcp/<slug>/code`, CodeGraph's tools for the same project, served by
the portal (see [The CodeGraph endpoint](../reference/mcp.md#the-codegraph-endpoint)). When you
[set up a project](projects.md#set-up) and select agents, the portal writes two config entries
into each agent's project file, `whygraph` and `codegraph`, each pointing at its endpoint over HTTP.
Your agent then has the same [tools, resources and prompts](../reference/mcp.md) it always had; the
server name is unchanged, so `mcp__whygraph__*` tool names still work, and the graph tools are
`mcp__codegraph__*`.

The portal must be running for an agent to reach it - there is no per-session process to launch any
more.

!!! note "Linked projects use the same files"
    A checkout [linked to a platform](platform-projects.md) gets exactly the same entry and assets as a
    local-folder project, with the platform project's slug in the URL, so teammates linked to the same
    project share one committed entry. The agent still talks to **your local portal**, never to the
    platform: the local portal answers from your working tree and the platform's history.

## What gets written

All four agents are project-scoped: the file lives in your repository, so teammates can share it.

| Agent | Config file | Entries |
|---|---|---|
| Claude Code (`claude`) | `.mcp.json` | `"whygraph": {"type": "http", "url": "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/<slug>"}` and `"codegraph"` with the same shape and `/mcp/<slug>/code`, under `mcpServers` |
| Cursor (`cursor`) | `.cursor/mcp.json` | `"whygraph": {"url": "http://127.0.0.1:8765/mcp/<slug>"}` and `"codegraph": {"url": "http://127.0.0.1:8765/mcp/<slug>/code"}` under `mcpServers` |
| VS Code / Copilot (`vscode`) | `.vscode/mcp.json` | `"whygraph": {"type": "http", "url": "http://127.0.0.1:${input:whygraph-port}/mcp/<slug>"}` and a `"codegraph"` entry with `/code`, under `servers`, plus one `inputs` entry that prompts for the port (both use it), default `8765` |
| Codex (`codex`) | `.codex/config.toml` | `[mcp_servers.whygraph]` with `url = "http://127.0.0.1:8765/mcp/<slug>"` and `[mcp_servers.codegraph]` with `/code`; both tables carry `tool_timeout_sec = 120` |

Both entries are written together, so a file gets one backup, one diff and one confirmation for the
pair. The Codex timeout is there because a CodeGraph call may wait for the index to catch up (see
[Freshness](../guide/mcp-usage.md#freshness)).

Each agent also gets its bundled assets - skills, commands and subagents that teach it when to call
WhyGraph - copied into `.claude/`, `.cursor/`, `.github/` or the repo root and `.codex/agents/`, and a
CodeGraph usage-guidance block appended to its always-on instructions (`CLAUDE.md`, `AGENTS.md`,
`.github/copilot-instructions.md`, or an always-apply rule for Cursor). The guidance tells the agent to
prefer the portal's `codegraph_*` tools over grep, and not to run the `codegraph` command itself.

### Keeping the agent files up to date { #agent-files-upgrade }

The portal records what it installed in `.whygraph/assets.json` (gitignored with the rest of
`.whygraph/`): the WhyGraph version and a hash of every file it wrote, and, for the block in a shared
file such as `AGENTS.md`, of the text between the markers. **Update agent files** in the project's
Settings (and re-initializing) uses it per file:

| The file on disk | Result |
|---|---|
| Missing | Written |
| Still the way WhyGraph wrote it | Updated silently to this version's copy |
| You edited it, and this version's copy is the same as the one you started from | Left alone |
| You edited it, and this version's copy changed | Left alone and listed as **Edited**, with the difference |
| Installed before the manifest existed (a teammate's checkout, an older release) | Treated as edited |

Tick **Overwrite my edits too** (shown only when edited files are listed) to replace those as well.
Settings shows `Agent files: v<installed> (current v<package>)` with an **Update** button when the
recorded version is behind, and the project's Overview lists "Agent files are out of date" the same way.
Removing an agent deletes only its MCP entry; the installed files and the manifest stay as they are.

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
agent entries by what each agent supports. Both entries (`whygraph` and `codegraph`) follow the same
way, so each item below is one line per entry:

| Agent | What to do after a port change |
|---|---|
| Claude Code | Export `WHYGRAPH_PORT=<port>` in the environment you start Claude Code from. The committed file needs no edit |
| VS Code | Enter the new port when the editor prompts for it |
| Cursor | Edit the port in `.cursor/mcp.json`. The portal rewrites it for you when the file is not tracked by git |
| Codex | Edit the port in `.codex/config.toml`. The portal rewrites it for you when the file is not tracked by git |

A file tracked by git is **never rewritten automatically** on a port change - committed configs
belong to the whole team. After a port change the **Projects** page shows a notice listing what the
portal changed, the exact lines to change by hand in each tracked file (the `whygraph` line, then the `codegraph` one;
with a copy button), the
environment hints for Claude Code and VS Code, and any project folders it could not reach (they still
name the old port until the portal starts with the folder available). Each project's overview and
settings show that project's part. Dismissing a notice hides it until the port changes again. The
same report is in the `port_change` field of `GET /api/portal/state` and of
`GET /api/projects/<slug>`.

## Agent-specific notes

**Claude Code** asks you to approve a project-scoped MCP server the first time it sees it. Approve
both `whygraph` and `codegraph` when prompted (or with `/mcp`); nothing connects until you do. After
**Update agent files** adds the `codegraph` entry to a project you set up earlier, Claude Code asks
again for the new server, unless you once chose to approve all future servers in the project (`claude mcp
reset-project-choices` forgets that choice). Each `mcp__codegraph__*` tool also asks for permission the
first time in a project. To allow them without the prompt, add `"mcp__codegraph__*"` to
`permissions.allow` in the untracked `.claude/settings.local.json`; the portal does not write that file.

**VS Code** also reads a workspace-root `.mcp.json`. A repository configured for both Claude Code and
VS Code therefore lists the `whygraph` and `codegraph` servers twice in VS Code. That is noisy, not
broken; disable one of each pair.

**Codex** loads `.codex/config.toml` only for projects you have marked as trusted. Trust the project
in Codex for both servers to load.

## Reviewing a diff (Claude Code) { #whygraph-review }

Claude Code gets one more slash command, `/whygraph-review [<base>] [--generate]`. It checks a diff
against the recorded rationale: for each hunk it finds the enclosing symbol with CodeGraph, reads that
symbol's rationale card, and reports exactly one verdict - **Constraint at risk** or **Risk triggered**
(each quoting the card verbatim), **No rationale**, or **Consistent** - followed by a
`## Confidence: high|medium|low` line. It runs as the `whygraph-reviewer` subagent, which may run only
`git diff` and writes no files.

- **What it reviews.** With no `<base>`, your uncommitted changes against `HEAD`; with a clean working
  tree, the branch against its merge base with the default branch (`origin/HEAD`, then `main`, then
  `master`). With a `<base>`, the working tree against the commit where your branch left it. Untracked
  files are new code and have no rationale, so they are left out; so are binary and deleted files. At
  most 40 hunks are reviewed.
- **No LLM spend by default.** It reads only cards that are already cached (`cached_only`), and a hunk
  without one reports **No rationale**. `--generate` lets WhyGraph generate the missing cards, one LLM
  call each, charged to the project's [budget](usage.md); a `budget_exceeded` refusal stops further
  generation for the rest of the review.
- **A card matches exact lines.** A card is cached for a symbol's line range and the commits blamed on
  it. An edit that changes the symbol's length, or a commit that lands on its lines, means the earlier
  card no longer matches, so that hunk reports **No rationale** until a card is made for the code as it
  stands (`--generate`, or the next `/rationale` or `/whygraph-plan` that reaches it).
- **Linked projects.** On a checkout [linked to a platform](platform-projects.md) the platform serves the
  cards and does not honour `cached_only`, so without `--generate` the reviewer does not ask for cards
  at all: every hunk with a symbol reports **No rationale** and confidence is `low`. The one evidence
  lookup it makes to tell that the project is linked may let the platform describe one commit, under
  the platform's description limit. With `--generate` it asks the platform, which may generate cards
  under its own budget.
- **Other agents.** Cursor, VS Code and Codex do not get the reviewer yet. Cursor also loads
  `.claude/skills/`, so with Claude Code selected `/whygraph-review` shows up in Cursor's slash menu too;
  it is untested there.

## Connect your agent outside a project

You do not need to open a project first. On a production portal the **Connect your agent** button on
the Projects page checklist, and on the banner a new member sees, opens a short dialog: install
WhyGraph and run `whygraph up` on your machine, choose one of the projects you can read (a lone project
is chosen for you), then follow the **Use with your agent** link for it. With no project yet the dialog
says to open a project and use that card there. A local portal's checklist sends you to the first
project's Overview instead, where the agent card lives.

## Agent activity

The portal counts how often agents call WhyGraph, per project, so the project's **Overview** can show
whether an agent is actually connected and what it asks for. What is counted:

- every MCP **tool call**, **resource read** and **prompt** on a project's `/mcp/<slug>` endpoint,
  under the tool, resource or prompt name (for example `whygraph_evidence_for` or `whygraph_commit`);
- every call to one of the four CodeGraph tools on `/mcp/<slug>/code` (`codegraph_explore`,
  `codegraph_node`, `codegraph_search`, `codegraph_impact`), which the Overview groups as **Code
  exploration**. A call to any other name is refused and never counted;
- on a production portal, every **data call** a [connected portal](platform-projects.md) makes for one
  of its agents (evidence, rationale, area history, a commit, a pull request, an issue, the repository
  overview).

A call counts even when the answer came from the cache. Protocol chatter (listing tools, resources or
prompts), the linked-project **status check** a local portal makes, a call refused by a rate limit,
and the Explorer and Chat views are not agent activity and are not counted.

The counter stores **counts only**: the project, the UTC day, the source (MCP or a connected portal),
who made the call and through which connection, the call's name, and how many calls. Never a path, a
symbol name, an argument or anything from the answer. The names are WhyGraph's own or one of the four
CodeGraph tools. Counts are written to the portal database
every 30 seconds, so a crash can lose at most the last 30 seconds of counts, and they are kept 400
days. A call that made an LLM request is also in the [usage ledger](usage.md), with its cost.

The Overview's Agent activity card also carries a line about the hosted CodeGraph server: its state,
process id and uptime, when the index was last refreshed and how long that took, and how many calls
waited for the index, timed out or were refused since the portal started. Those tallies live in memory
and are not part of the stored counts.

## Removing or changing agents

In the project's **Settings**, the Agents section reconfigures which agents are connected. Removing a
project can also strip the `whygraph` and `codegraph` entries the portal wrote, under the same backup
and tracked-file rules, and leaves every other server in those files alone. A `codegraph` entry that
is not the portal's own (one whose URL does not end in `/mcp/<slug>/code`) is never touched.

## Manual entry

If you would rather not let the portal edit a file - or it refused one - the entry to paste for each agent is shown
in the **Connect your agent** panel on the project's home page.

## If you installed CodeGraph yourself { #own-codegraph }

CodeGraph's own installer may already have wired your machine to a host-installed server. The portal
serves the same tools, pinned to each project, so you can drop the host install. Initialize looks for
what is left and reports it, and changes none of it without your word:

- **A `codegraph` entry of your own** (a stdio command, not the portal's URL) in an agent file is left
  exactly as it is, with a warning. Tick **Replace with the portal's CodeGraph** in the preview to swap
  it for the portal's entry.
- **CodeGraph's instruction section** (between its `CODEGRAPH_START` markers) in `CLAUDE.md`,
  `AGENTS.md` or `.github/copilot-instructions.md` is reported and left alone; delete it by hand once
  the portal's guidance block is in.
- **CodeGraph's own git hooks** (the sync hook in `post-commit`, `post-merge` or `post-checkout`) are
  reported and left alone. They are harmless but redundant with the portal's hooks.

To remove a global install, run `codegraph uninstall --keep-cli` on your machine: it removes the user
level server entry, the prompt hook and the instruction section and keeps the command. For Claude Code,
also run `claude mcp remove codegraph -s local` if you added the server for one project; a project
entry already shadows a global one. Codex reads a global `~/.codex/config.toml` too, and reads the
project's file only once you [trust the project](#agent-specific-notes).

To stop serving the tools from the portal while keeping your own server, turn **Serve CodeGraph tools
to agents** off in the project's Settings under Scanning (`[scan].serve_codegraph = false`). The tools
then answer with a "turned off" error and the Overview says so.

!!! warning "Do not run a host CodeGraph on the same repository"
    A second CodeGraph writing the same index is invisible to the portal's lock. While it holds
    CodeGraph's own file lock, the portal's refresh can report success without doing anything, so the
    portal's tools may answer from an index that is not current until the next change.

## Troubleshooting

- **The agent says it cannot connect.** Check `whygraph status`: the portal has to be running, on the
  port in the entry.
- **`409` from the endpoint.** The project is added but not initialized yet. Finish setup in the
  portal.
- **A CodeGraph tool says the project has no code index yet.** Run a scan from the project's Overview;
  the first scan builds the index. The tools never build one themselves.
- **A CodeGraph tool says the code index is still updating.** It names how many seconds to wait. Call
  the same tool again after that; the refresh keeps running in the background. See
  [Freshness](../guide/mcp-usage.md#freshness).
- **CodeGraph is paused on this portal after repeated crashes.** The one shared server stopped after
  crashing again and again, for every project. Use **Restart CodeGraph** on a project's Overview, or
  call again after the number of seconds the message names; **Open CodeGraph log** shows why it crashed.
- **The CodeGraph tools are turned off for this project.** `[scan].serve_codegraph` is `false`; turn it
  back on under Settings > Scanning.
- **CodeGraph is not available on this portal.** There is no `codegraph` command on the portal's `PATH`
  (only seen when you run the portal natively for development).
- **Indexing is refused because a symbolic link leaves the repository.** The message names the links.
  Remove them and scan again; see the [security model](security.md#symbolic-links).
- **A 1.x entry that runs `whygraph-mcp`.** That command was removed; it prints a message and exits. Add
  the repo in the portal and migrate the entry. See [Upgrading from 1.x](upgrading.md#from-1x).
