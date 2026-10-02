# Backup and restore

The portal's own data - the project list, settings, encrypted keys and tokens, and scan history - is
in its Postgres database, in the `whygraph-portal-postgres` container, with the database's files in
`postgres/` under the data directory (`~/.local/share/whygraph`). That database is what to back up.

Your projects' evidence, descriptions, rationale cache and chat history are **not** in it: they stay in
each repository, in `.whygraph/whygraph.db` and `.codegraph/`, and are backed up with the repository.

## Make a backup

```bash
whygraph backup
```

```console
$ whygraph backup
portal database backed up to /home/you/.local/share/whygraph/backups/portal-20261001T120000Z.dump
restore needs this data dir's secret.key too - back it up with the dump
```

It runs `pg_dump` inside the database container, so it needs that container running (`whygraph up`)
but not the portal, and the dump is always made by the same Postgres version as the database. The
portal can keep running while it does: the dump is a consistent snapshot.

- Dumps go to `backups/` in the data directory (mode `0700`), named `portal-<UTC time>.dump`, in
  Postgres's custom format (`pg_dump -Fc`).
- The **newest 10** `portal-<UTC time>.dump` files are kept, and older ones are deleted. Only files with
  exactly that name are counted or deleted: a dump you renamed, or any other file you put in
  `backups/`, is left alone.
- A failed dump leaves no partial file behind.

To keep a backup elsewhere, copy the dump **and** `secret.key` off the machine (see below).

### The automatic dump before an upgrade

When `whygraph up` is about to recreate a **running** database container - a new release moved the
pinned Postgres image, or you changed `WHYGRAPH_POSTGRES_IMAGE` - it runs the same backup first,
before touching either container. If that dump fails, `up` stops with both containers exactly as they
were. To recreate the database container without the dump (a full disk, a database too broken to
dump), say so explicitly:

```bash
WHYGRAPH_SKIP_BACKUP=1 whygraph up
```

The automatic dumps share the newest-10 retention with the ones you make.

## Back up `secret.key` with the dumps

!!! warning "A dump without its `secret.key` cannot give you your keys back"
    The API keys and GitHub tokens in the database are encrypted with `secret.key`, in the data
    directory. The dump holds only the encrypted form. Restore a dump with a different or a new
    `secret.key` and every stored key and token is unreadable: you would enter them all again.

Back up `secret.key` once, alongside your dumps; it does not change. Keep the two as safe as the live
data directory: a dump together with `secret.key` gives anyone who has them your stored keys.

## Restore { #restore }

A restore replaces everything in the portal database with the dump. Below, `DATA` is the data
directory; `IMAGE` and `MAJOR` are the database image and its Postgres major version, which
`whygraph status` shows on its `database:` line (for example `postgres:18.6-trixie`, major `18`):

```bash
DATA=~/.local/share/whygraph
IMAGE=postgres:18.6-trixie   # from `whygraph status`
MAJOR=18
DUMP="$DATA/backups/portal-20261001T120000Z.dump"
```

1. **Stop the portal and its database:**

    ```bash
    whygraph down
    ```

2. **Make sure the data directory has the `secret.key` that belongs to the dump.** Restore it from
   your backup if needed.
3. **Start only the database container**, and wait until it accepts connections:

    ```bash
    docker run -d --name whygraph-portal-postgres --user "$(id -u):$(id -g)" \
        --mount "type=bind,source=$DATA/postgres,target=/var/lib/postgresql" \
        -e "PGDATA=/var/lib/postgresql/$MAJOR/docker" \
        "$IMAGE"
    until docker exec whygraph-portal-postgres pg_isready -q -h 127.0.0.1 -U whygraph -d whygraph; do sleep 1; done
    ```

4. **Restore the dump:**

    ```bash
    docker exec -i whygraph-portal-postgres \
        pg_restore -U whygraph -d whygraph --clean --if-exists --single-transaction --no-owner \
        < "$DUMP"
    ```

    `--single-transaction` makes it all or nothing: on an error, the database is left as it was.
    `pg_restore` runs inside the database image, which must be at least as new as the `pg_dump` that
    wrote the archive - true for any dump `whygraph backup` made with this or an older release.

5. **Remove the hand-started container and start WhyGraph normally:**

    ```bash
    docker stop whygraph-portal-postgres && docker rm whygraph-portal-postgres
    whygraph up
    ```

On a new machine, run `whygraph up` once first, so the database exists, then follow the steps above;
step 2 is where the old machine's `secret.key` replaces the new one.

## Copying the whole data directory

The other way to back up is a plain copy of the data directory, but only under two conditions:

- **WhyGraph is stopped** (`whygraph down`). Copying the files of a running Postgres gives a backup that
  may not start.
- **It is restored with the same Postgres major version.** The files are not readable by another major;
  a dump is.

```bash
whygraph down
cp -a ~/.local/share/whygraph /somewhere/safe/whygraph-data
whygraph up
```

To restore, `whygraph down`, put the copy back in place of the data directory, and `whygraph up`. A
copy holds `secret.key` and `postgres.password` too, so it is as sensitive as the live data
directory. Prefer dumps: they are smaller, can be made while WhyGraph runs, and survive a Postgres
upgrade.

## Removing WhyGraph

To remove WhyGraph, and with it this data, see
[Start the portal](start.md#removing-whygraph). Take a backup first if you may want the portal's data
back.
