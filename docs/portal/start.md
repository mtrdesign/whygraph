# Start the portal

You need the [Docker install](../getting-started/installation.md) - the portal runs in the WhyGraph
image, and the `whygraph` shim on your `PATH` starts and stops it.

```bash
whygraph up
```

That starts two named containers in the background - `whygraph-portal`, the portal itself, and
`whygraph-portal-postgres`, the Postgres database it keeps its own data in - waits until the database is
ready, and prints the portal's address:

```console
$ whygraph up
whygraph portal running at http://127.0.0.1:8765
```

Open <http://127.0.0.1:8765>. Both containers restart with Docker (`--restart unless-stopped`), so you
start them once, not per session. The database container has no published port: only the portal can
reach it, over their private Docker network. Idle, it uses about 30 MiB of RAM.

!!! note "Native installs cannot run the portal"
    `pip` / `uv` installs provide headless `whygraph scan` only. The portal needs the Docker install.
    Running `whygraph portal` natively exists for WhyGraph development and is not supported for
    everyday use; see the [CLI reference](../reference/cli.md#whygraph-portal).

## First run

The first time you open the portal it shows a **Welcome** screen. Enter your name and continue - that
creates the single local user. The screen also shows the portal's mode (**Local**) and the folders it
can see, and carries the [shared-machine note](security.md#shared-machines).

You land on an empty Projects page, and it is a short **checklist** instead of a blank one:

1. **Add an LLM key** - under **Settings**, so descriptions, rationale cards and Chat have a model.
2. **Add a project** - [share a folder](shared-folders.md) that holds your repositories if you have not
   yet, then [add a project](projects.md).
3. **Connect your agent** - from the project's Overview, once it exists.

Each step has one button and ticks itself off when it is done. **Dismiss** hides the checklist in this
browser, and **Getting started** in the Projects header brings it back; it disappears for good when
every step is done. Projects can be added before a key is saved - the first scan never calls an LLM.

## The host commands

These run on your host, through the shim, not inside a container of their own.

| Command | What it does |
|---|---|
| `whygraph up [--port N] [--add-folder DIR]` | Start the portal and its database, or recreate the portal when the folders, port or image changed. Already running with nothing changed is a no-op. |
| `whygraph down` | Stop and remove both containers, the portal first. It waits up to 30 seconds so a running scan is stopped cleanly and recorded as interrupted. |
| `whygraph status` | Each container's state (running, restarting or not created), plus the URL, image, database image and shared folders. |
| `whygraph logs` | Follow the portal container's logs. The database has its own: `docker logs whygraph-portal-postgres`. |
| `whygraph folders [--remove DIR]` | List the shared folders, or remove one (which recreates the portal container). |
| `whygraph backup` | Dump the portal database into the data directory. See [Backup and restore](backup.md). |

Stopping the portal never touches your repositories or the data directory. Start it again and
everything is where you left it.

## Choosing a port

The default is `8765`. To use another one:

```bash
whygraph up --port 9000
```

The port is remembered in `~/.config/whygraph/port`, so a later plain `whygraph up` keeps it. Without
`--port` and without that file, the shim reads the exported `WHYGRAPH_PORT`, then falls back to
`8765`.

A port change also has consequences for the repositories you already added: their markers and some
agent config entries carry the port. The portal rewrites what it safely can when it starts and
reports, per project, what is left. See [Connecting agents](agents.md#a-non-default-port).

## Where your data goes

| Path on the host | Holds |
|---|---|
| `~/.local/share/whygraph` (override with `WHYGRAPH_DATA`) | Everything the portal keeps: see the next table |
| `~/.config/whygraph/folders` | One shared folder per line |
| `~/.config/whygraph/port` | The port chosen with `--port` |

Inside the data directory:

| Path | Holds |
|---|---|
| `postgres/` | The portal database's files (projects, settings, encrypted keys, scan history), one subdirectory per Postgres major version |
| `postgres.password` | The database password, generated on the first `whygraph up` (mode `0600`) |
| `secret.key` | The encryption key for the keys and tokens stored in the database |
| `backups/` | Database dumps from `whygraph backup` and the automatic pre-upgrade dump |
| `runs/` | Scan progress and log files |
| `repos/` | Production mode only: the copies of the repositories imported from GitHub |

The data directory is created with mode `0700`, owned by you, and the database files in it are owned
by you too. Keep it out of every project folder: the portal refuses to share a folder that contains it
or sits inside it.

## Stopping and upgrading

`whygraph down` stops the portal and its database; the data stays in the data directory. Installing a
newer version (re-running the installer) changes the image the shim uses; the next `whygraph up`
notices the new image and recreates the portal container, with the same data. When a release also
moves the pinned Postgres image, `up` first dumps the running database into `backups/`, then recreates
the database container too. See [Upgrading](upgrading.md).

## Removing WhyGraph

There is no uninstall command. To remove WhyGraph from a machine:

1. Remove each project in the portal and let it strip what it wrote to the repository (the git hooks
   and markers; see [Adding projects](projects.md)). Your repositories keep their `.whygraph/` and
   `.codegraph/` data either way.
2. `whygraph down`.
3. Delete the data directory (`~/.local/share/whygraph`), `~/.config/whygraph`, and the `whygraph`
   and `whygraph-mcp` scripts the installer put on your `PATH` (`~/.local/bin` by default).
4. Optionally, remove the images (`docker image ls ghcr.io/mtrdesign/whygraph`, then
   `docker image rm` the ones listed, and the same for `postgres`) and the network
   (`docker network rm whygraph-portal`).

Take a [backup](backup.md) first if you may want the portal's data back.

## Environment credentials do not reach the portal

Nothing from your shell environment is passed into the container. `GH_TOKEN`, `GITHUB_TOKEN`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` and `OPENROUTER_API_KEY` set on your host
are ignored by the portal - **enter keys and tokens under Settings** in the portal instead.

The first `whygraph up` that finds one of these variables set prints a one-time note naming them (never
their values).

## If something is in the way

- **A leftover `whygraph-serve` container** from 1.x may hold port 8765. `whygraph up` warns about it;
  remove it with `whygraph serve --stop`.
- **A crash-looping portal** shows as `restarting` in `whygraph status`; `whygraph logs` has the reason.
  A portal that cannot reach its database within a minute exits, and Docker retries it; check the
  `database:` line of `whygraph status` and `docker logs whygraph-portal-postgres`.
- **`up` says the database did not become ready.** No portal is started; `docker logs
  whygraph-portal-postgres` has the reason.
- **`up` refuses a database another Postgres major version created**, after a release moved the pin.
  Follow the [dump and restore recipe](upgrading.md#postgres-major).
- **`up` says `postgres.password` is missing but the database exists.** Restore the file from your
  backup of the data directory. A new password would not open the existing database.
- **A second portal on the same data directory, or on the same database,** is refused: the portal
  holds an exclusive lock on both.
