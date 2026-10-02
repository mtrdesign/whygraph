# Upgrading

To upgrade, re-run the [installer](../getting-started/installation.md), then `whygraph up`. What else
happens depends on where you come from:

- **From 1.x**, you add your repositories to the portal; their data is reused. See
  [From 1.x](#from-1x).
- **A release that moves the pinned Postgres major version** needs a dump and a restore. See
  [Postgres major versions](#postgres-major).

## Postgres major versions { #postgres-major }

Each Postgres major version (18, 19, ...) has its own on-disk format, so the database files keep one
subdirectory per major, `postgres/<major>/`. When a release moves the pin to a new major and the data
directory holds only an older one, `whygraph up` refuses and starts nothing:

```text
whygraph: the portal database in .../postgres was created by Postgres 18; this release needs 19.
```

Move the data across with a dump made by the old major and a restore into the new one. `OLD` and `NEW`
below are the two majors, and `DATA` is the data directory:

```bash
DATA=~/.local/share/whygraph
OLD=18
NEW=19
```

1. **Dump with the old major.** If `whygraph status` shows `database: running`, the old container is
   still up: run `whygraph backup`. Otherwise, start the old major by hand, wait until it accepts
   connections, and back it up:

    ```bash
    docker run -d --name whygraph-portal-postgres --user "$(id -u):$(id -g)" \
        --mount "type=bind,source=$DATA/postgres,target=/var/lib/postgresql" \
        -e "PGDATA=/var/lib/postgresql/$OLD/docker" \
        "postgres:$OLD-trixie"
    until docker exec whygraph-portal-postgres pg_isready -q -h 127.0.0.1 -U whygraph -d whygraph; do sleep 1; done
    whygraph backup
    ```

    `whygraph backup` runs `pg_dump` inside that container, so the dump is made by the old major.
2. **Stop everything:** `whygraph down`.
3. **Move the old cluster aside**, and keep it until the new one is confirmed working:

    ```bash
    mv "$DATA/postgres/$OLD" "$DATA/postgres-$OLD.old"
    ```

4. **Create the new, empty database:** `whygraph up`, then `whygraph down` again.
5. **Restore the dump into it**, following steps 3 to 5 of [Restore](backup.md#restore) with the new
   release's image and `NEW` as the major.
6. **Start the portal:** `whygraph up`. Once everything is there, delete `$DATA/postgres-$OLD.old`.

The restore runs in the new major's image on purpose: `pg_restore` must be at least as new as the
`pg_dump` that wrote the archive.

## From 1.x { #from-1x }

2.0.0 replaces per-repo setup with the [portal](index.md). **Your data carries over**: each repository
keeps its `.whygraph/whygraph.db` and `.codegraph/` index, and the portal reuses them. What changes is
how you start WhyGraph, where configuration and keys live, and how your agents connect.

### Breaking changes

| 1.x | 2.0.0 |
|---|---|
| `whygraph init` | Removed. Add the repo in the portal; it initializes it. |
| `whygraph serve` | Removed. The portal is the web panel, for all your projects at once. |
| `whygraph analyze` | Removed. Per-commit descriptions come from `scan` and from **Describe now**. |
| `whygraph-mcp` (stdio server) | Removed. Agents connect over HTTP to `/mcp/<slug>` on the portal. The `whygraph-mcp` command remains only to print a message. |
| `whygraph scan` in any repo | Still works headless, but **refuses in a repository the portal manages** - scan from the portal. |
| Hooks ran a local scan | Hooks ask the running portal to scan. They need the portal running; commits made while it was down are caught up when it next starts. |
| API keys and GitHub tokens from your shell environment | **No longer reach WhyGraph** in the portal. Enter them under Settings. |

`whygraph init`, `whygraph serve` and `whygraph-mcp` still exist as stubs: they print a pointer to the
portal and exit with status `2`, so an old habit, a stale script or a 1.x agent config that runs
`whygraph-mcp` fails with a message instead of a bare "not found". `whygraph analyze` is simply gone.

### Upgrade steps

1. **Install 2.x** with the [installer](../getting-started/installation.md), then stop any old
   playground: `whygraph serve --stop` removes a leftover `whygraph-serve` container that may hold port
   8765.
2. **Start the portal** and [share the folder](shared-folders.md) that holds your repositories:

    ```bash
    whygraph up --add-folder ~/Work
    ```

3. **Add each repository** from the Projects page, exactly like a new project. The portal detects what
   1.x left behind and reports it:

    - an existing `.whygraph/whygraph.db` is kept and migrated (with a backup in
      `.whygraph/backups/`) when you initialize;
    - 1.x git hooks are recognized and replaced by the portal's versions;
    - `whygraph` entries in agent config files are listed, with a choice per file to **migrate** the
      entry to HTTP (the default) or **remove** it.
4. **Enter your keys** under Settings. Provider keys and tokens that were in the repo's `whygraph.toml`
   are moved into the portal's encrypted store when the file is imported; ones that were only in your
   shell environment are not found and must be entered by hand.
5. **Reconfigure the agents.** The portal rewrites the config as HTTP. Claude Code will ask you to
   approve the server; Codex needs the project marked as trusted. See
   [Connecting agents](agents.md).

### Credentials from your shell environment

!!! warning "Enter keys under Settings"
    Credentials from your shell env no longer reach WhyGraph - enter them under Settings. The portal
    container is started with none of your environment, so exporting `ANTHROPIC_API_KEY` or `GH_TOKEN`
    has no effect on it or on the scans it runs. The first `whygraph up` names any such variables it
    sees so you know to move them.

Headless `whygraph scan` outside the portal (CI, a plain checkout) still reads the standard variables;
see [Configuration](../reference/configuration.md#environment-variables).

### Custom database paths

The portal always uses `<repo>/.whygraph/whygraph.db` and `<repo>/.codegraph/codegraph.db`. If a 1.x
`whygraph.toml` set `whygraph_db` or `codegraph_db` to somewhere else, the portal will not use it and
says so when you add the project. Move the file to the default location **before you initialize** to
keep your descriptions, rationale cache and chat history.

### Configuration keys

1.x keys still work through 2.x with a deprecation warning and are removed in 3.0; see
[Deprecated keys](../reference/configuration.md#deprecated-keys). A 1.x `whygraph.toml` resolves to the
same providers, models and timeouts it did before.

### Going back to 1.x

Your history stays in your repositories, and the project database is backed up before it is migrated.
To use a repository without the portal again, remove it from the Projects page (or delete
`.whygraph/portal.json`) and headless `whygraph scan` works in it once more.
