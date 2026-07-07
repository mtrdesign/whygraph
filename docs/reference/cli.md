# CLI reference

Every WhyGraph command and its flags. Run `whygraph <command> --help` to see the same text from your
own install. There are five commands.

```console
$ whygraph --help
Commands:
  analyze  Describe a commit's diff with the configured LLM.
  hooks    Manage opt-in git hooks that auto-rescan on new commits.
  init     Initialize the WhyGraph database under .whygraph/whygraph.db.
  scan     Run the source crawlers, then describe each commit with the LLM.
  version  Print installed whygraph version.
```

## `whygraph version`

Print the installed package version. No options.

```bash
whygraph version
```

## `whygraph init`

Bootstrap the WhyGraph database under `.whygraph/whygraph.db`, write a committable
`whygraph.example.toml` documenting every tunable, and add the right `.gitignore` entries. It's
idempotent - re-running on an initialized project just confirms both databases are present.

On a terminal, `init` runs a guided, arrow-key setup: pick the agent, the analyze/rationale LLMs
(with optional API keys), and the source-control provider (with an optional GitHub token). It shows a
summary that masks every secret, asks *"Write these files?"*, then writes both `whygraph.example.toml`
(secret-free) and a ready-to-run `whygraph.toml` (with the secrets you entered). Every prompt is
defaulted. `--yes` (and any non-TTY invocation) skips the prompts, uses defaults, and never clobbers
an existing `whygraph.toml`.

`init` does **not** index CodeGraph. That happens on [`scan`](#whygraph-scan).

With `--agent X`, it also wires the WhyGraph MCP server into that agent's config. All supported
agents are project-scoped, so the config file is written inside the repo.

| Option | Description |
|---|---|
| `--agent [claude\|codex\|copilot\|cursor\|vscode]` | Wire the MCP server into the named agent's config. On a terminal, skips the interactive agent prompt. |
| `--yes` / `-y` | Accept all defaults without prompting (also implied off a TTY). Writes a default `whygraph.toml` only if none exists. |
| `--print` | Print the MCP snippet to stdout instead of writing any config file. |
| `--list-agents` | List supported agents (with config-file paths) and exit. |
| `--install-assets / --no-install-assets` | Copy the chosen agent's bundled assets into the project. Default: enabled. No-op for agents that ship no asset tree. |
| `--skip-preflight` | Skip the host-tool diagnostics that normally run first. For known-good scripted environments. |
| `--force` | When installing assets, overwrite existing files in the agent's destination directory. |

See [Wiring your editor](../guide/editors.md) for the per-agent paths.

## `whygraph scan`

Run the source crawlers, then describe each commit with the configured LLM. This is the command that
populates `.whygraph/whygraph.db` and refreshes the CodeGraph index. It's idempotent - re-running
picks up new commits and backfills what's missing.

| Option | Default | Description |
|---|---|---|
| `--no-llm-descriptions` | off | Skip the per-commit LLM description phase. The git and GitHub crawlers still run; descriptions backfill lazily on demand and on a later full scan. |
| `--codegraph / --no-codegraph` | on | Refresh the CodeGraph index concurrently with the crawl - `codegraph sync` when an index exists, `codegraph init -i` on first run. A failure here warns rather than aborting. |
| `--codegraph-image TEXT` | pinned tag | Override the Docker image used for the CodeGraph refresh fallback. Ignored when a local `codegraph` binary is found. |
| `--remote / --no-remote` | on | Crawl the source-control remote (GitHub PRs / issues) per `[scan].provider`. `--no-remote` skips it for a fast, offline, token-free scan. |
| `--pr-origins / --no-pr-origins` | on | Recover a squash-merged PR's original feature-branch commits via one targeted `git fetch`. Needs the network, so it's skipped under `--no-remote`. |

See [Scanning your repo](../guide/scanning.md) for what each phase does.

## `whygraph analyze`

Describe a single commit's diff with the configured LLM and **print** the result. Unlike `scan`, it
doesn't persist anything.

```bash
whygraph analyze <TARGET> [BASELINE]
```

`TARGET` is the commit being analyzed. With no `BASELINE`, it's compared to its parent; with a
`BASELINE`, the diff analyzed is `git diff BASELINE..TARGET`.

!!! note "Scan first"
    Every commit named on the command line must already exist in the WhyGraph database. Run
    `whygraph scan` before `whygraph analyze`.

## `whygraph hooks`

Manage opt-in git hooks that auto-rescan on new commits. There's no daemon - the hooks run a fast,
background, offline scan as you commit.

| Subcommand | Description |
|---|---|
| `install` | Install the auto-rescan hooks into the current repository. Idempotent and non-clobbering - it appends to a foreign hook behind a sentinel guard. |
| `status` | Report whether the auto-rescan hooks are installed. |
| `uninstall` | Remove the auto-rescan hooks, leaving any foreign hook content intact. |

```bash
whygraph hooks install
```

The hooks wire `post-commit`, `post-merge`, and `post-rewrite` to run
`whygraph scan --no-remote --no-llm-descriptions` in the background. See
[Keep it fresh](../guide/scanning.md#keep-it-fresh) for the details.
