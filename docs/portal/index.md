# The portal

The portal is WhyGraph's front door. It is **one long-running server** that holds all your projects,
shows each one in the Explorer and the Chat assistant, runs every scan, and serves each project's
evidence and rationale to your coding agents over HTTP MCP at `/mcp/<slug>`.

You start it once with `whygraph up`, open it in a browser, and add repositories from the Projects
page - a folder you share, or a checkout you link to a project on a team's
[platform](platform-projects.md). There is no per-repo setup command: the portal does what `whygraph init` used to do, with a
preview of every file it will touch.

```mermaid
flowchart LR
    browser["Browser<br/>(Explorer + Chat)"]
    agents["Claude Code, Cursor,<br/>VS Code, Codex"]
    subgraph portal["whygraph-portal (Docker, 127.0.0.1 only)"]
        api["API + MCP<br/>/api, /mcp/&lt;slug&gt;"]
        runner["Scan runner"]
    end
    subgraph pg["whygraph-portal-postgres (Docker, no published port)"]
        db[("Portal database<br/>projects, settings,<br/>encrypted keys, scan history")]
    end
    repos[("Your repos<br/>(shared folders)")]

    browser --> api
    agents -- "HTTP MCP" --> api
    api --> db
    api --> runner
    runner -- "whygraph scan" --> repos
    api -- "reads .whygraph + .codegraph" --> repos
    hooks["git hooks"] -- "POST /api/projects/&lt;slug&gt;/scans" --> api
```

## What lives where

| Thing | Where |
|---|---|
| Portal settings, project list, API keys and GitHub tokens (encrypted), scan history | The portal database: Postgres in the `whygraph-portal-postgres` container, its files in `postgres/` under the portal's data directory, `~/.local/share/whygraph` on your host |
| The encryption key for those keys and tokens, and database backups | The data directory, as `secret.key` and `backups/` |
| The [usage ledger](usage.md): one row per LLM call (counts, cost, model, who and which project - never prompt or chat text), budgets, and price overrides | The portal database, kept 400 days |
| A project's evidence, descriptions, rationale cache and chat history | The repository itself, in `.whygraph/whygraph.db` |
| A project's CodeGraph index | The repository itself, in `.codegraph/` |
| A [platform project](platform-projects.md)'s history, descriptions and rationale | The platform, never your machine. Your portal keeps only the link: the platform's address, the project's name and a **connection token**, encrypted |
| The folders, port and image the portal was started with | `~/.config/whygraph` on your host |

Because each project's databases stay in its own repository, removing a project from the portal
never deletes your history, and a 1.x repository keeps the data it already has. See
[Upgrading](upgrading.md). The portal's own database is the one thing to back up; see
[Backup and restore](backup.md).

## Where to next

<div class="grid cards" markdown>

-   :material-play-circle-outline:{ .lg .middle } __Start the portal__

    ---

    `whygraph up`, the first-run setup, and the other host commands.

    [:octicons-arrow-right-24: Start the portal](start.md)

-   :material-folder-network-outline:{ .lg .middle } __Shared folders__

    ---

    The portal runs in Docker and only sees the folders you share with it.

    [:octicons-arrow-right-24: Shared folders](shared-folders.md)

-   :material-source-repository:{ .lg .middle } __Adding projects__

    ---

    A repository from a shared folder, configure, initialize, first scan.

    [:octicons-arrow-right-24: Adding projects](projects.md)

-   :material-lan-connect:{ .lg .middle } __Projects from a platform__

    ---

    Link a checkout to a team's WhyGraph so your agent reads its history, and your own changes stay local.

    [:octicons-arrow-right-24: Projects from a platform](platform-projects.md)

-   :material-connection:{ .lg .middle } __Connecting agents__

    ---

    The MCP entry each agent gets, and what to know about ports and approval prompts.

    [:octicons-arrow-right-24: Connecting agents](agents.md)

-   :material-cash-multiple:{ .lg .middle } __Usage & cost__

    ---

    What your LLM calls cost, per project, model and member, and the budgets that cap them.

    [:octicons-arrow-right-24: Usage & cost](usage.md)

-   :material-shield-lock-outline:{ .lg .middle } __Security model__

    ---

    Loopback only, where keys live, and what the portal will not do to your repo.

    [:octicons-arrow-right-24: Security model](security.md)

-   :material-database-arrow-down-outline:{ .lg .middle } __Backup and restore__

    ---

    `whygraph backup`, the automatic dump before an upgrade, and how to restore.

    [:octicons-arrow-right-24: Backup and restore](backup.md)

-   :material-update:{ .lg .middle } __Upgrading__

    ---

    From 1.x, add your repos like new ones.

    [:octicons-arrow-right-24: Upgrading](upgrading.md)

</div>
