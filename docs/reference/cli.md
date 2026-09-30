# CLI reference

Every WhyGraph command and its flags. Run `whygraph <command> --help` to see the same text from your
own install. There are four commands in the image, plus the host verbs the Docker shim adds and the
stubs that stand in for removed ones.

```console
$ whygraph --help
Commands:
  install  Emit the host shim installer (called by scripts/install.sh).
  portal   Run the WhyGraph portal (use `whygraph up` to start it in Docker).
  scan     Run the source crawlers, then describe each commit with the LLM.
  version  Print installed whygraph version.
```

## Host commands

On the Docker install the `whygraph` shim handles these **on the host**, before any container of its own
runs. They manage the portal's long-lived container; they are not Python subcommands. The full
walkthrough is [Start the portal](../portal/start.md).

| Command | What it does |
|---|---|
| `whygraph up [--port N] [--add-folder DIR]` | Start the portal container, or recreate it when the shared folders, port or image changed. `--port` is remembered in `~/.config/whygraph/port`; `--add-folder` (repeatable) shares a folder. No-op when already running unchanged. |
| `whygraph down` | Stop and remove the container, waiting up to 30 seconds (`WHYGRAPH_STOP_GRACE` overrides) so a running scan is recorded as interrupted. |
| `whygraph status` | Running, restarting or not created, plus the URL, image and shared folders. Exits non-zero unless running. |
| `whygraph logs` | Follow the container's logs. |
| `whygraph folders [--remove DIR]` | List the shared folders, or remove one and recreate the container. |

Any other argument to these verbs is rejected with exit code 2. Port precedence for `up` is the
remembered `--port`, then `WHYGRAPH_PORT`, then `8765`. `up` never passes your environment into the
container, and on the first run that sees a credential variable set it prints its name once, as a
reminder to enter the key under Settings.

## `whygraph version`

Print the installed package version. No options.

```bash
whygraph version
```

## `whygraph scan`

Run the source crawlers, then describe each commit with the configured LLM. This is the command that
populates `.whygraph/whygraph.db` and refreshes the CodeGraph index. It's idempotent - re-running
picks up new commits and backfills what's missing.

In the portal you don't run it yourself: the portal runs `whygraph scan --progress json` as a child
process for every scan (see [Scans in the portal](#scans-in-the-portal)). Run it directly for **headless**
use - a CI job or a checkout the portal does not manage. It reads the repo's `whygraph.toml` (or
`WHYGRAPH_CONFIG_JSON`) and the standard provider environment variables, and creates
`.whygraph/whygraph.db` on first run.

!!! warning "It refuses in a portal-managed repository"
    When the repo root has a valid, untracked `.whygraph/portal.json` marker - written when the portal
    initializes a project - `whygraph scan` exits with status `2` before touching any config or
    database, and prints:

    ```text
    This project is managed by the WhyGraph portal (http://127.0.0.1:<port>/p/<slug>). Run scans from
    the portal. To use this repo without the portal, remove it from the Projects page, or delete
    .whygraph/portal.json.
    ```

    A marker that git tracks, that is a symbolic link, or that is malformed is ignored with a warning,
    and the scan goes ahead.

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

The portal never follows a symbolic link out of a project's folder, since a repository's content
(especially a GitHub clone's) is not trusted. When `.whygraph/`, `.codegraph/`, either database,
`.gitignore`, `whygraph.toml`, a portal marker, an agent config file or a bundled agent folder
(such as `.claude/`) is a symlink, the portal refuses it with an `unsafe_path` error: Initialize,
the Explorer, Chat, the MCP endpoint and scans all stop for that project, a `whygraph.toml` link is
not imported, and removing the project leaves the linked files alone. Replace the link with a real
file or folder to continue.

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

The portal's HTTP API exposes the same run data the web UI shows:

| Endpoint | Returns |
|---|---|
| `GET /api/projects/<slug>/scans` | The project's 50 most recent runs, newest first |
| `GET /api/projects/<slug>/scans/<id>/events` | The run's progress as a server-sent event stream (resumable with `Last-Event-ID`) |
| `GET /api/projects/<slug>/scans/<id>/log` | The end of the run's log, at most the last 64 KiB, starting at a line boundary: `{run_id, text, size, truncated}`. Keys are already masked. |
| `GET /api/portal/state` | Portal status, including the installed `version` (what `whygraph version` prints) |

Every `/api` call needs the `X-WhyGraph-Client: 1` header, and the scans endpoints answer `409`
until the project is initialized.

## Removed commands

2.0.0 removed four entry points. The portal replaces them, and an old habit or a stale script gets a
message rather than "no such command".

| Removed | Was | Now |
|---|---|---|
| `whygraph init` | Bootstrap the database, write config, wire an agent | Add the repo on the portal's Projects page; it initializes it |
| `whygraph serve` | The single-repo web panel | The [portal](../portal/index.md), for all your projects |
| `whygraph-mcp` | The stdio MCP server | The portal's HTTP endpoint at `/mcp/<slug>` |
| `whygraph analyze` | Describe one commit and print it | Gone; descriptions come from `scan` and **Describe now** |

`whygraph init`, `whygraph serve` and `whygraph-mcp` still exist as **stubs**: each prints a pointer to
`whygraph up` on **stderr** and exits with status `2`. The `whygraph-mcp` stub writes nothing to
stdout, so an agent that still launches it over stdio reads nothing it could misparse, and it is a
plain script that never starts Docker. `whygraph serve --stop` is the one remaining use of the old
verb: it removes a leftover 1.x `whygraph-serve` container. `whygraph analyze` has no stub.

See [Upgrading from 1.x](../portal/upgrading.md).

## `whygraph install`

!!! info "Plumbing, not a step you run"
    This command exists **inside the image** so the installer can call it. Installing WhyGraph is
    the `curl … | sh` line in [Installation](../getting-started/installation.md) - you don't run
    `whygraph install` yourself.

Prints the POSIX `sh` script that writes the `whygraph` shim and the `whygraph-mcp` removal stub onto
your `PATH`, pinned to the image's baked version. `scripts/install.sh` runs it via
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
