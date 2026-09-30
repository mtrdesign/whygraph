# Start the portal

You need the [Docker install](../getting-started/installation.md) - the portal runs in the WhyGraph
image, and the `whygraph` shim on your `PATH` starts and stops it.

```bash
whygraph up
```

That starts one named container, `whygraph-portal`, in the background and prints its address:

```console
$ whygraph up
whygraph portal running at http://127.0.0.1:8765
```

Open <http://127.0.0.1:8765>. The container restarts with Docker (`--restart unless-stopped`), so you
start it once, not per session.

!!! note "Native installs cannot run the portal"
    `pip` / `uv` installs provide headless `whygraph scan` only. The portal needs the Docker install.
    Running `whygraph portal` natively exists for WhyGraph development and is not supported for
    everyday use; see the [CLI reference](../reference/cli.md#whygraph-portal).

## First run

The first time you open the portal it shows a **Welcome** screen. Enter your name and continue - that
creates the single local user. The screen also shows the portal's mode (**Local**) and the folders it
can see, and carries the [shared-machine note](security.md#shared-machines).

You land on an empty Projects page. [Share a folder](shared-folders.md) that holds your repositories
if you have not yet, then [add a project](projects.md).

## The host commands

These run on your host, through the shim, not inside a container of their own.

| Command | What it does |
|---|---|
| `whygraph up [--port N] [--add-folder DIR]` | Start the portal, or recreate it when the folders, port or image changed. Already running with nothing changed is a no-op. |
| `whygraph down` | Stop and remove the container. It waits up to 30 seconds so a running scan is stopped cleanly and recorded as interrupted. |
| `whygraph status` | Running, restarting or not created, plus the URL, image and shared folders. |
| `whygraph logs` | Follow the container's logs. |
| `whygraph folders [--remove DIR]` | List the shared folders, or remove one (which recreates the container). |

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
| `~/.local/share/whygraph` (override with `WHYGRAPH_DATA`) | The portal database, the encryption key for stored secrets, GitHub clones, scan run files |
| `~/.config/whygraph/folders` | One shared folder per line |
| `~/.config/whygraph/port` | The port chosen with `--port` |

The data directory is created with mode `0700`, owned by you. Keep it out of every project folder: the
portal refuses to share a folder that contains it or sits inside it.

## Stopping and upgrading

`whygraph down` stops the portal. Installing a newer version (re-running the installer) changes the
image the shim uses; the next `whygraph up` notices the new image and recreates the container, with
the same data.

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
- **A second portal on the same data directory** is refused: the portal holds an exclusive lock on it.
