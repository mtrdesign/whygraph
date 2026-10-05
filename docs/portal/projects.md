# Adding projects

A **project** is one repository the portal knows about. Add one from the **Projects** page with
**New project**. A project has one of three **sources**:

| Source | Where it lives | Where it is added |
|---|---|---|
| **Local folder** | A repository in a [shared folder](shared-folders.md) on your machine, scanned by this portal | Local mode, **New project**, from a folder |
| **A platform** | A checkout on your machine **linked** to a project on a [production](../deploy/production.md) WhyGraph; its history stays there | Local mode, **New project**, **From a platform** - see [Projects from a platform](platform-projects.md) |
| **GitHub** | A repository the portal cloned itself, through a GitHub App | [Production mode](../deploy/production.md#projects) only |

The rest of this page describes the local-folder wizard, with up to four steps: pick the repository,
configure it, initialize it, and run the first scan.

!!! note "Production mode imports from GitHub"
    In [production mode](../deploy/production.md#projects) projects are imported from GitHub through
    the GitHub App instead, and the wizard is Source, Configure and First scan.

!!! note "A linked project is different after the first step"
    A platform project has no Configure step and no local database: its models, keys and history are
    the platform's, its Explorer and Chat open there, and its first scan builds the CodeGraph index only.
    The only setting it keeps is which git hooks rescan the checkout. Initializing it writes the same
    agent files as any project.

## Pick a repository

Choose a repository from the list of those under your [shared folders](shared-folders.md), or type a
path. The portal checks that the path is under a shared folder and is a git repository. Local mode
does not clone: to work on a GitHub repository, clone it into a shared folder yourself and add that.

Adding a local repository **writes nothing into it**. It only registers the project, reads an
existing `whygraph.toml` to offer its settings, and reports what 1.x left behind (see
[Upgrading from 1.x](upgrading.md#from-1x)).

If the origin is on GitHub you can paste a token here so the remote crawl (pull requests and issues)
can work; it is checked against GitHub before anything is stored.

The project's name defaults to the repository name and becomes its **slug** (lowercase letters,
digits and hyphens), which appears in URLs and in the MCP endpoint. The slug cannot change later; the
display name can.

## Configure

The second step sets how this project uses LLMs and GitHub:

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
    passed to the portal or to its scans. Enter them under Settings (or in a project's configure step).
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

!!! note "`claude-cli` in the portal: your Claude subscription"
    The image ships the `claude` CLI, but not your login (on macOS it lives in the Keychain). To bill
    your Claude subscription, run `claude setup-token` once in a terminal on your machine, then paste
    the token it prints under **Settings > Provider keys > Claude subscription token** (globally, or
    for one project). The portal stores it encrypted and hands it only to `claude` itself. Until one
    is set, a project whose model is `claude-cli` shows "no key for claude-cli", and a scan skips the
    commit descriptions with that message.

### A `whygraph.toml` in the repository

If the repository has a `whygraph.toml`, the portal offers its **settings** as the project's starting
configuration - models, the tuning keys, `[scan].forge`, `remote`, `default_branch` and `hooks`. API
keys and the GitHub token in it are moved into the portal's encrypted store, and the wizard lists the
lines you can now delete from the file. Things the portal will not take from a repository file are
reported and dropped: endpoints (`base_url`, `host`), database paths, the log file and similar. After
the import the file is no longer read for this project.

## Initialize

The third step previews, then does, what makes the project ready. **Nothing is written until you
confirm**, and the preview lists every file:

- `.gitignore` gains `whygraph.toml`, `.whygraph/` and `.codegraph/`.
- The [auto-rescan git hooks](../guide/scanning.md#keep-it-fresh) are installed (local projects).
- For each agent you select, an HTTP MCP entry is written to that agent's config file, and the
  agent's bundled assets (skills, commands, subagents) are copied in. See
  [Connecting agents](agents.md).
- The project database is created, or migrated if it is from 1.x (with a backup first).
- Two small marker files, `.whygraph/portal.json` and `.whygraph/portal.env`, record that the
  portal manages this project. They are gitignored.

A file that is tracked by git is shown as a diff and needs an explicit confirmation before it is
touched. A file the portal cannot parse safely (JSONC with comments, TOML with comments) is **refused**
and the exact entry to paste is shown instead.

## First scan

The last step runs the first scan, with live progress. The first scan reads git history and refreshes
the CodeGraph index; it **never calls an LLM**, so it costs nothing and finishes quickly.

Afterwards the project shows how many commits a full scan would describe, with which model, and a
token estimate (and a cost for known models). Choose **Describe now** to spend on per-commit
descriptions, or **Later** to let them backfill on demand. See [Scanning](../guide/scanning.md).

## After setup

| Where | What you do |
|---|---|
| **Explorer** | Browse the graph, evidence and rationale ([The Explorer](../guide/playground.md)) |
| **Chat** | Ask questions ([The Chat assistant](../guide/chat.md)) |
| **Scans** | See each run's progress and log; start **Scan now**, or **Cancel** a queued or running run |
| **Settings** | Change configuration, reconfigure agents, or remove the project |

**Cancel** asks first. A queued run just leaves the queue; a running one stops within about ten
seconds and is recorded as *Cancelled by you*. Commits it had already described are kept, so the next
scan picks up where it stopped.

Removing a project offers to strip what the portal wrote: the git hooks, the markers, and the `whygraph`
entries in agent config files it created. It never deletes your repository, its `.whygraph/` data, or
(for a local repo) anything else.

## When something is wrong

The project page explains these states instead of failing quietly:

- **The folder is not available** - not shared any more, moved, or deleted. Share it again or remove
  the project.
- **No longer a git repository** - the `.git` folder is gone.
- **Not set up yet** - added but never initialized; **Finish setup** resumes the wizard.
- **A symbolic link where a real file belongs** - see the [security model](security.md#symbolic-links).
