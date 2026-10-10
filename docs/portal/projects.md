# Adding projects

A **project** is one repository the portal knows about. Add one from the **Projects** page with
**New project**. A project has one of three **sources**:

| Source | Where it lives | Where it is added |
|---|---|---|
| **Local folder** | A repository in a [shared folder](shared-folders.md) on your machine, scanned by this portal | Local mode, **New project**, from a folder |
| **A platform** | A checkout on your machine **linked** to a project on a [production](../deploy/production.md) WhyGraph; its history stays there | Local mode, **New project**, **From a platform** - see [Projects from a platform](platform-projects.md) |
| **GitHub** | A repository the portal cloned itself, through a GitHub App | [Production mode](../deploy/production.md#projects) only |

The rest of this page describes the local-folder wizard, in three steps: **Source** (pick the
repository), **Set up** (connect your agents; this starts the first scan) and **Configure** (what
commit descriptions need, while the first scan runs). On a phone the stepper reads "Step 2 of 3 -
Set up".

!!! note "Production mode imports from GitHub"
    In [production mode](../deploy/production.md#projects) projects are imported from GitHub through
    the GitHub App instead, and the wizard is **Source** and **Configure**: the import clones and scans
    the repository in the background, and there is nothing to set up on the server's copy.

!!! note "A linked project is different after the first step"
    A platform project's wizard is **Source** and **Set up**: it has no Configure step and no local
    database. Its models, keys and history are the platform's, its Explorer and Chat open there, and
    its first scan builds the CodeGraph index only. The only setting it keeps is which git hooks rescan
    the checkout. Setting it up writes the same agent files as any project.

## Pick a repository

Choose a repository from the list of those under your [shared folders](shared-folders.md) (the
search matches the name or any part of the path; the list shows the first 500 matches, so refine the
search to find one past them), or type a path. The portal checks that the path exists, is under a
shared folder and is a git repository; a folder outside the shared ones shows the
`whygraph up --add-folder` command that shares it. Local mode does not clone: to work on a GitHub
repository, clone it into a shared folder yourself and add that.

Adding a local repository **writes nothing into it**. It only registers the project, reads an
existing `whygraph.toml` to offer its settings, and reports what 1.x left behind (see
[Upgrading from 1.x](upgrading.md#from-1x)).

If the origin is on GitHub you can paste a token here so the remote crawl (pull requests and issues)
can work; it is checked against GitHub before anything is stored. You can also add it later, on the
Configure step.

The project's name defaults to the repository name and becomes its **slug** (lowercase letters,
digits and hyphens), which appears in URLs and in the MCP endpoint. The slug cannot change later; the
display name can.

## Set up

The second step previews, then does, what makes the project ready (the portal calls this
*initializing* the project). **Nothing is written until you confirm**, and the preview lists every
file:

- `.gitignore` gains `whygraph.toml`, `.whygraph/` and `.codegraph/`.
- The [auto-rescan git hooks](../guide/scanning.md#keep-it-fresh) are installed (local projects).
- For each agent you select, an HTTP MCP entry is written to that agent's config file, and the
  agent's bundled assets (skills, commands, subagents) are copied in: `.mcp.json` and `.claude/` for
  Claude Code, `.cursor/` for Cursor, `.vscode/mcp.json` and `.github/` for VS Code, and
  `.codex/config.toml`, `AGENTS.md` and `.codex/agents/` for Codex. Each card names the port the
  entry uses - the portal's own. See [Connecting agents](agents.md).
- The project database is created, or migrated if it is from 1.x (with a backup first).
- Two small marker files, `.whygraph/portal.json` and `.whygraph/portal.env`, record that the
  portal manages this project. They are gitignored.

A file that is tracked by git is shown as a diff and needs an explicit confirmation before it is
touched. A file the portal cannot parse safely (JSONC with comments, TOML with comments) is **refused**
and the exact entry to paste is shown instead.

**Set up project** also queues the **first scan**, so it starts even if you close the tab, and the
wizard moves on to Configure. If the scan queue refuses it, the done panel says why and offers
**Start the first scan**. Setting the project up again later (from its settings, under Agents) queues
nothing.

## Configure

The last step is one screen you can read while the first scan runs:

- **The first scan's progress**: one bar, a status line ("Reading git history - 1,240 of 5,300
  commits"), the scan's steps with their counts, and **Show details** to the scan's own page. The
  first scan reads git history and refreshes the CodeGraph index; it **never calls an LLM**, so it
  costs nothing and finishes quickly. The step follows that scan after a reload, too.
- **What descriptions need**: the model that describes commits, and whether its provider key is in
  place. No key is fine - descriptions can wait; add one here now or later in settings.
- **GitHub pull requests and issues**: when the repository is on GitHub and no token is in effect,
  a token field. Saving it also asks for a quick rescan, which runs right after the first scan, so
  pull requests and issues arrive without a full rescan.
- **More settings** opens the project's settings (chat model, hooks, limits).
- **The estimate**, once the first scan has read the history: how many commits a full scan would
  describe, with which model, the tokens and an estimated cost. The cost is priced for every provider
  that has a price (Anthropic and OpenAI out of the box, any other model once your organization adds
  a price), at the bundled prices or your organization's own; a model with no price shows tokens
  only. Someone without access to the project's spend sees the number of commits without a price.
  An empty history says "No commits yet".

**Open project** leaves the wizard at any time - the scan carries on. **Describe N commits (~$X)**
starts the full scan that writes the descriptions, once the first scan has finished and you may spend
on this project; otherwise it says why it is disabled. Spend is tracked on [Usage & cost](usage.md);
descriptions you skip backfill on demand. See [Scanning](../guide/scanning.md).

## Models, keys and GitHub

A project's **Settings** set how it uses LLMs and GitHub (the wizard's Configure step links there as
**More settings**):

| Section | What you set |
|---|---|
| **Models** | A default model, with optional overrides for per-commit descriptions, rationale cards and chat |
| **Provider keys** | One key per provider. Keys are **write-only**: the page shows `set ...a1b2` and never the key. Providers without a key show a "no key" badge while a task resolves to them |
| **Endpoints** | A base URL for OpenAI-compatible gateways, and the Ollama host. Changing an endpoint clears the key stored for it - a key must not follow you to a different server |
| **GitHub** | A token, and whether to crawl pull requests and issues at all |
| **Git hooks** | Which of `post-commit`, `post-merge`, `post-rewrite` and `post-checkout` refresh the project |

Every project inherits the defaults from **Settings** in the portal sidebar: models, provider keys
and endpoints you set there apply to all projects unless a project overrides them. Set your keys once
there and most projects need no configuration of their own.

!!! warning "Credentials from your shell environment no longer reach WhyGraph"
    `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GH_TOKEN` and the rest, exported in your shell, are **not**
    passed to the portal or to its scans. Enter them under Settings (or on a project's Configure step).
    They are stored encrypted in the portal database, not in your repository.

### Ollama and gateways on your machine

The portal runs in a container, so `localhost` inside it is the container, not your host. To reach an
Ollama daemon or an OpenAI-compatible gateway running on your machine, use **`host.docker.internal`**:

| Setting | Value |
|---|---|
| Ollama host | `http://host.docker.internal:11434` |
| OpenAI-compatible base URL | `http://host.docker.internal:<port>/v1` |

The portal container is started with that name mapped to your host, on Linux as well as macOS and
Windows.

### A `whygraph.toml` in the repository

If the repository has a `whygraph.toml`, the portal offers its **settings** as the project's starting
configuration - models, the tuning keys, `[scan].forge`, `remote`, `default_branch` and `hooks`. API
keys and the GitHub token in it are moved into the portal's encrypted store. Things the portal will not take from a repository file are
reported and dropped: endpoints (`base_url`, `host`), database paths, the log file and similar, and
any setting for the removed [`claude-cli` provider](../reference/llm-providers.md#claude-cli-removed).
The project's settings list the lines you can now delete from the file. After the import the file
is no longer read for this project.

## The Projects list

The **Projects** page lists every project you can see, as cards. Each card shows:

- the project's name and where it lives: the folder (relative to its shared folder) locally, the
  GitHub repository in production, the platform and project for a linked one;
- a **status** pill (the same one the project's Overview shows, see [Health](#health)): **Ready**,
  **Scanning**, **Importing**, **Needs setup**, **Behind**, **Scan failed**, **Import failed**,
  **Folder missing**, **No access**, **Link revoked**, **Stopped** (the monthly budget is reached) or
  **Not supported**, and when it was last scanned (hidden while a scan runs, an import runs, or the
  folder is missing);
- the counts as of the last scan, for example "1,240 commits · 38% described · 52 symbols explained";
- this month's spend with a thin bar against the project's budget, for people who can see the
  project's usage;
- a grey **Viewer** or **Contributor** badge when that is your role, **Restricted** for a restricted
  project, and the source (**Local**, **GitHub**, **Platform**) only when the organization mixes
  sources.

From two projects up, a search box filters by name or repository, and **Sort** orders the cards by
Name, Last scanned, Status (problems first) or Cost (when costs are shown). Both stay in the address,
so a link keeps them. The page shows 60 cards, then **Show more**. **New project** (locally) or
**Import** (in production) stays in the header whenever you may add one.

The card's **...** menu holds **Quick rescan**, **Full rescan** (project admins) and **Settings**.
Each rescan says what it does - a quick rescan reads git history and the code index with no LLM cost,
a full rescan also describes new commits, with the estimated cost when the menu opens - and a rescan
that cannot run says why instead (for example "Monthly budget reached", "No Anthropic key" or "Finish
setting up this project first").

With no projects yet, owners and admins (and the local user) see how to add the first one; a member
sees that nobody has shared a project with them yet, and an instance administrator viewing an
organization sees that it has none.

## The Overview

A project's **Overview** is its home page. The header names the status, your role when you are not a
project admin ("Your role: Contributor"), and where the project lives - the GitHub repository (a link)
in production, the folder locally. Its buttons follow the project's state: **Open Explorer** and the
rescan menu when it is healthy, **Reconnect** only for a linked project whose link was revoked, and a
disabled control always says why it is disabled.

### Health

The health panel lists what needs attention, most important first, each with what fixes it:

| Item | What you can do |
|---|---|
| A linked project's link was revoked or removed, or the platform is unreachable | **Reconnect**, or remove it from this machine |
| GitHub no longer lets WhyGraph read the repository (production) | **Reinstall the GitHub App** or **Check repository access** (owners and admins) |
| The import is running, or did not finish | **Follow the import**; **Retry import** and **Open log** |
| The folder is not available, or is no longer a git repository | Share the folder again (the command is shown), or remove the project |
| A symbolic link where a real file belongs | See the [security model](security.md#symbolic-links) |
| **Setup not finished** | **Finish setup** resumes the wizard |
| The monthly budget is reached and its hard stop is on | See usage and budgets |
| No key for the model that describes commits, writes rationale cards or chats | **Add a key** (project admins) |
| The last scan failed | **Retry** and **Open log** |
| The checkout is ahead of the last scan | **Rescan** |
| Commits have no description yet | **Describe** (project admins); they also fill in as you browse |

With nothing to report it says **Healthy**. An import ranks above "folder missing", because an
imported repository has no folder until its clone lands.

### Tiles, coverage and usage

Five tiles count **Commits**, **Commits described** (with an LLM description; the share and "N of M"),
**Pull requests**, **Issues** and **Symbols explained** (symbols with a rationale card, as the
Explorer's coverage map shows them). Each has a one-line definition; a zero says why, for example "No
GitHub data: add a GitHub token" for a local project whose remote is on GitHub.

**Coverage over time** draws the share of described commits after each scan, with markers for imports
(production), first scans (local), full scans, descriptions, failed runs and budget stops. The history starts with this release, so a
project shows "History starts with the next scan" until it has two scans to compare.

When commits are waiting for a description, the Overview shows how many, with the estimated cost and
**Describe now** for project admins; everyone else sees the count without a price. People who can see
the project's usage also get **Usage this month**: the spend against the budget, split by task, with a
link to the project's usage and budgets.

### Agent activity

**Agent activity** counts the calls agents made to this project over the last 30 days: MCP calls
locally, calls from linked portals in production. It says how many used the LLM (and what they cost,
for people who see usage), which kinds of call they were ("Evidence lookups", "Rationale cards",
"Area history", ...), when the last one was and, in production, who made them and through which
connected portal. With no call in 30 days the section shows **Connect your agent** instead; see
[Agent activity](agents.md#agent-activity).

## After setup

| Where | What you do |
|---|---|
| **Explorer** | Browse the graph, evidence and rationale ([The Explorer](../guide/playground.md)) |
| **Chat** | Ask questions ([The Chat assistant](../guide/chat.md)) |
| **Scans** | See each run's progress and log; start a rescan, or **Cancel** a queued or running run |
| **Settings** | Change configuration, reconfigure agents, or remove the project |

In production, the rescan button is a menu: **Quick rescan** reads git history and refreshes CodeGraph
with no LLM, and **Full rescan** also describes commits. A contributor gets Quick only, and a viewer
neither; see [project roles](../deploy/production.md#project-roles). A project marked **Restricted** by
its admins carries a Restricted badge on its card and its home page, and is visible only to people with
access.

**Cancel** asks first. A queued run just leaves the queue; a running one stops within about ten
seconds and is recorded as *Cancelled by you*. Commits it had already described are kept, so the next
scan picks up where it stopped.

Removing a project offers to strip what the portal wrote: the git hooks, the markers, and the `whygraph`
entries in agent config files it created. It never deletes your repository, its `.whygraph/` data, or
(for a local repo) anything else.
