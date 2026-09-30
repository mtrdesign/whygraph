# CLI reference

Every WhyGraph command and its flags. Run `whygraph <command> --help` to see the same text from your
own install. There are seven commands.

```console
$ whygraph --help
Commands:
  analyze  Describe a commit's diff with the configured LLM.
  init     Initialize the WhyGraph database under .whygraph/whygraph.db.
  install  Emit the host shim installer (called by scripts/install.sh).
  portal   Run the WhyGraph portal (use `whygraph up` to start it in Docker).
  scan     Run the source crawlers, then describe each commit with the LLM.
  serve    Serve the WhyGraph web panel (Explorer + Chat) for this repository.
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
(with optional API keys), the source-control provider (with an optional GitHub token), and whether to
install the auto-rescan git hooks. It shows a summary that masks every secret, asks *"Write these
files?"*, then writes both `whygraph.example.toml` (secret-free) and a ready-to-run `whygraph.toml`
(with the secrets you entered). Every prompt is defaulted.

`init` also installs the auto-rescan git hooks and **reconciles them to `[scan].hooks` in both
directions** - installing what the config lists and stripping the managed block from what it
doesn't. Editing `[scan].hooks` and re-running `whygraph init` is the supported way to change hook
coverage; see [Keep it fresh](../guide/scanning.md#keep-it-fresh). A hooks directory that can't be
written is a warning, never a failed init.

`init` does **not** index CodeGraph. That happens on [`scan`](#whygraph-scan).

With `--agent X`, it also wires the WhyGraph MCP server into that agent's config. All supported
agents are project-scoped, so the config file is written inside the repo.

| Option | Description |
|---|---|
| `--agent [claude\|codex\|copilot\|cursor\|vscode]` | Wire the MCP server into the named agent's config. Case-insensitive. On a terminal, skips the interactive agent prompt. Run `whygraph init --help` for the full list, with each agent's config format and asset destination. |
| `--yes` / `-y` | Accept all defaults without prompting, and write a default `whygraph.toml` if none exists. |
| `--force` | When installing assets, overwrite existing files in the agent's destination directory. |

!!! note "`--yes` and a non-TTY run are not the same thing"
    Both skip the prompts, but only `--yes` writes `whygraph.toml`. A bare non-interactive `init` -
    a pipe, CI, the git hooks - refreshes `whygraph.example.toml` and leaves `whygraph.toml`
    untouched. Neither ever clobbers an existing one.

Preflight diagnostics always run. Asset install runs whenever an agent is chosen: **every** supported
agent ships a bundled asset tree, copied into its own destination (use `--force` to overwrite local
edits). Without `--agent`, `init` skips the wiring and prints a tip instead.

See [Wiring your editor](../guide/editors.md) for the per-agent paths.

## `whygraph scan`

Run the source crawlers, then describe each commit with the configured LLM. This is the command that
populates `.whygraph/whygraph.db` and refreshes the CodeGraph index. It's idempotent - re-running
picks up new commits and backfills what's missing.

| Option | Default | Description |
|---|---|---|
| `--skip-analyze` | off | Skip the per-commit LLM description phase. The git and GitHub crawlers still run; descriptions backfill lazily on demand and on a later full scan. |
| `--codegraph / --no-codegraph` | on | Refresh the CodeGraph index concurrently with the crawl - `codegraph sync` when an index exists, `codegraph init -i` on first run. A failure here warns rather than aborting. |
| `--codegraph-image TEXT` | pinned tag | Override the Docker image used for the CodeGraph refresh fallback. Ignored when a local `codegraph` binary is found. |
| `--remote / --no-remote` | on | Crawl the source-control remote (GitHub PRs / issues) per `[scan].forge`. `--no-remote` skips it for a fast, offline, token-free scan. |
| `--pr-origins / --no-pr-origins` | on | Recover a squash-merged PR's original feature-branch commits via one targeted `git fetch`. Needs the network, so it's skipped under `--no-remote`. |
| `--progress json` | off | Print machine-readable JSON lines on stdout instead of the Rich bars and panels; see [JSON progress](#json-progress). Logs and errors stay on stderr. |

See [Scanning your repo](../guide/scanning.md) for what each phase does.

### JSON progress

`--progress json` is for programs that drive a scan (the portal's scan runner, CI wrappers). Stdout
carries one JSON object per line, each written whole, so a reader can parse line by line. The
`type` key says what a line is:

| `type` | When | Fields |
|---|---|---|
| `start` | Always first | `phase_total` - the number of phases this run executes (2 to 4, depending on `--skip-analyze`, `--no-remote`, `--pr-origins` and whether an LLM is configured) |
| `phase` | Each phase begins | `phase` (1-based), `title` |
| `task` | A crawler registers, and as it progresses (throttled; a finished task is always sent) | `name` (stable crawler label), `completed`, `total` (`null` while unknown), `description` (current status text) |
| `result` | Always last | `status` (`ok` or `failed`), `elapsed_sec`, `phase_timings`, `crawlers` (per crawler: `name`, `status`, `summary`, and `error` / `warning` when set), `analyze_skipped` (why the LLM phase was skipped, else `null`) |

The exit code is `1` when any crawler failed, exactly as without the flag.

## `whygraph serve`

Serve the local web panel for this repository: the **Explorer** over the code graph, evidence, and
rationale, and the **Chat assistant** over the same data. On the Docker install it runs as its own
long-lived container, published to `127.0.0.1` only. Run `whygraph scan` first so there's an index
and evidence to show.

!!! warning "Serving starts the chat assistant too"
    Chat calls an LLM under `[chat]` and writes sessions and messages to the WhyGraph database. If
    you want a purely read-only panel, simply don't use the Chat view - but know that starting the
    server makes it reachable.

| Option | Default | Description |
|---|---|---|
| `--port` | `8765` | Port to bind. On the Docker install, set the port via the `WHYGRAPH_PORT` environment variable instead (the shim controls both the published and in-container port). |
| `--host` | `127.0.0.1` | Bind address. The Docker shim passes `0.0.0.0` for the container so the loopback port-forward can reach it; you rarely set this by hand. |

On the Docker install the shim also adds container-lifecycle verbs - these are **not** flags of the
Python command, they're handled on the host before the container starts:

| Command | What it does |
|---|---|
| `whygraph serve --detach` / `-d` | Start in the background and return immediately. |
| `whygraph serve --logs` | Tail the detached server's logs. |
| `whygraph serve --stop` | Stop and remove the running server. |
| `whygraph serve --help` | Show the in-container `serve --help`. |

Any other argument is **rejected** with `unknown arg` and exit code 2 - so `whygraph serve --port 9000`
fails on a Docker install rather than being silently ignored. Use `WHYGRAPH_PORT` instead. A stale
`whygraph-serve` container is force-removed before each start, so a crashed server never blocks the
next one.

See [The Explorer](../guide/playground.md) and [The Chat assistant](../guide/chat.md) for the panel
itself.

## `whygraph portal`

Run the multi-project **portal**: one long-running server that holds many projects, with the
Explorer and Chat per project and a per-project HTTP MCP endpoint at `/mcp/<slug>`. It is the
command the Docker runtime runs inside the image.

!!! warning "Development only when run natively"
    In this release the supported way to run the portal is the Docker runtime, which publishes it on
    `127.0.0.1` only. Run natively, `whygraph portal` is for development: shared folders come only
    from `WHYGRAPH_SHARED_FOLDERS`, and a non-loopback `--host` is refused unless `--dev-expose` is
    given, because local mode has no login.

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. Outside the image, anything but a loopback address needs `--dev-expose`. |
| `--port` | `$WHYGRAPH_PORT` or `8765` | Port to bind. It is also the port the browser and agents use, so the allowed `Host` / `Origin` values and the agent MCP URLs are built from it. |
| `--data DIR` | `$WHYGRAPH_DATA` or `~/.local/share/whygraph` | Portal data directory: the portal database, the encryption key, cloned repositories. Keep it outside every project folder. |
| `--dev-expose` | off | Allow a non-loopback `--host` outside the image. |

The portal is a single process and holds an exclusive lock on its data directory, so a second
`whygraph portal` on the same directory exits with code 2. Every `/api` request must carry the
`X-WhyGraph-Client: 1` header and a loopback `Host`; cross-site requests and foreign `Origin` values
are rejected. `WHYGRAPH_DEV_ORIGINS` (comma-separated origins, e.g. `http://localhost:5173`) adds
origins for a local frontend dev server.

### Scans in the portal

The portal runs every scan itself, as a `whygraph scan --progress json` child process in the
project's folder. Each project scans one run at a time; a request that arrives while a run is going
joins the single queued run instead of starting another, so a burst of commits costs one follow-up
scan. At most two projects scan at once.

| Trigger | Started by | LLM descriptions |
|---|---|---|
| `initial` | Any request while the project has no successful scan yet | No (structure only) |
| `hook` | A git hook after a commit, merge, rebase or checkout, and the catch-up check | No, and offline (`--no-remote`) |
| `sync`, `poll` | The **Sync** button, and a poll every 15 minutes, for projects cloned from GitHub | No |
| `manual` | **Scan now** | Yes (unless turned off for that run) |
| `describe` | **Describe now** on the first-scan estimate | Yes |

The first scan never calls the LLM. Afterwards the project shows how many commits a full scan would
describe, with which model and a token (and, for known models, cost) estimate, so you choose when
to spend. A sync fetches the default branch, fast-forwards the checkout and rescans only when that
moved it. When the portal starts, and on every poll, it also rescans a local project whose checkout
moved past the last scanned commit while the portal was not running.

Only an allowlist of the portal's environment reaches a scan (`PATH`, `HOME`, locale, `TZ`, the TLS
bundle and proxy variables); API keys and GitHub tokens come only from the portal's own settings,
and never appear in run files: each run's progress (`runs/<id>.jsonl`) and log (`runs/<id>.log`)
under the data directory show a key as its last four characters. Stopping the portal stops running
scans and records them as `interrupted`.

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

## `whygraph install`

!!! info "Plumbing, not a step you run"
    This command exists **inside the image** so the installer can call it. Installing WhyGraph is
    the `curl … | sh` line in [Installation](../getting-started/installation.md) - you don't run
    `whygraph install` yourself.

Prints the POSIX `sh` script that writes the `whygraph` and `whygraph-mcp` shims onto your `PATH`,
pinned to the image's baked version. `scripts/install.sh` runs it via
`docker run --rm IMAGE whygraph install` and executes the output; keeping the shim bodies here (in
tested Python) rather than in the fetched shell is what stops the two from drifting.

```bash
whygraph install
```

No options. Two reasons you might still invoke it directly:

| Command | Why |
|---|---|
| `docker run --rm ghcr.io/mtrdesign/whygraph:1.1.2 whygraph install` | Read exactly what would be written to your `PATH`, without writing it. |
| `docker run --rm ghcr.io/mtrdesign/whygraph:1.1.2 whygraph install \| sh` | Install with no `curl` - air-gapped hosts, CI images. |
