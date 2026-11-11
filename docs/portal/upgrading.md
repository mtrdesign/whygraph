# Upgrading

To upgrade, run `whygraph update`. It checks GitHub for the newest release, downloads that release's
installer, dumps the database, installs the new `whygraph` command and image, and recreates the portal
with `whygraph up`, in that order:

```bash
whygraph update             # update to the newest release
whygraph update --check     # only say whether there is one
whygraph update --version X.Y.Z   # a specific release
```

- **Nothing is changed until the installer has downloaded.** A typo in `--version`, or a host that is
  offline, stops before the backup, so it never pushes a real dump out of the
  [newest-10 retention](backup.md#make-a-backup).
- **The dump comes first.** If it fails, nothing was installed; `WHYGRAPH_SKIP_BACKUP=1 whygraph update`
  goes on without it. See [Backup and restore](backup.md#the-automatic-dump-before-an-upgrade).
- **No downgrades.** Database migrations only go forward, so an older release is refused; restore a
  [backup](backup.md#restore) instead.
- **A release that needs manual steps stops here.** If the target, or any release between yours and the
  target, is marked as needing them (a Postgres major bump, for instance), `update` prints the release
  notes and this page and exits with status `2`. Follow them, then re-run with `--force`. When the
  releases in between cannot all be checked, `update` treats the release the same way.
- **A release published less than an hour ago may not have its image yet.** If the install fails with a
  message that the version does not exist, wait a few minutes and run `whygraph update` again.
- **With a registry mirror,** the new `whygraph` command pins `ghcr.io/mtrdesign/whygraph:<version>`.
  If that image is not on the machine, `update` installs but does not start the portal, and prints the
  `docker tag` command that names your mirror's image as the pinned one; then run `whygraph up`.
- **A symlinked `whygraph` command** is replaced by a regular file in the link's directory.
- **Dev portals and overrides are refused.** `update` does nothing when `WHYGRAPH_IMAGE`,
  `WHYGRAPH_PORTAL_NAME` or `WHYGRAPH_DEV_SRC` is set, and it needs `curl`.

!!! note "An install from before `whygraph update` existed"
    A `whygraph` command installed before 2.3.0 has no `update` verb: it answers
    `No such command 'update'`. Upgrade that one time with the manual path below; from then on
    `whygraph update` works.

The portal itself never updates anything: it can only tell you a release is out, see
[The update notice](#update-notice). The manual path still works: re-run the
[installer](../getting-started/installation.md), then `whygraph up`. What else happens depends on where
you come from:

- **From 1.x**, you add your repositories to the portal; each starts with empty history. See
  [From 1.x](#from-1x).
- **From 2.0.0**, `whygraph scan` is gone and each project's history moves out of its repository into
  the portal's data directory; the old in-repo database is not read, so history starts empty. See
  [WhyGraph 2.1.0](../releases/2.1.0.md#breaking-changes).
- **From 2.1.0**, nothing to do: each project's database is upgraded on first use, with a backup kept
  beside it for rolling back, and the first full scan walks the GitHub history once more. See
  [WhyGraph 2.2.0](../releases/2.2.0.md#fixes).
- **From 2.2.0**, nothing to do: 2.2.1 changes only the portal's UI. See
  [WhyGraph 2.2.1](../releases/2.2.1.md).
- **From 2.2.1**, re-run the installer and `whygraph up` this once (2.2.1 has no `whygraph update`);
  each project's database and the portal's database are upgraded on first use. See
  [WhyGraph 2.3.0](../releases/2.3.0.md).
- **From 2.3.0**, `whygraph update`; each project's database is upgraded on first use. `[analyze].max_workers`
  now defaults to 4, so a project that never set it describes twice as many commits at once. See
  [WhyGraph 2.4.0](../releases/2.4.0.md#changes-to-know-about).
- **Agent files in your repositories** are not rewritten by an upgrade. Open each project's Settings >
  Agents and use **Update agent files**, once per project: it adds the `codegraph` MCP entry, files you
  have not edited update silently and edited ones are listed first (a project set up before the
  manifest existed asks about each file once). See [Keeping the agent files up to date](agents.md#agent-files-upgrade).
- **If you installed CodeGraph on your host**, the portal now serves its tools, so you can remove the
  global install with `codegraph uninstall --keep-cli`. See
  [If you installed CodeGraph yourself](agents.md#own-codegraph).
- **The first scan of each project after upgrading rebuilds its code index** once, in full, so that
  symbols read through a symbolic link that leaves the repository are dropped. See the
  [security model](security.md#symbolic-links).
- **A release that moves the pinned Postgres major version** needs a dump and a restore. See
  [Postgres major versions](#postgres-major).

## The update notice { #update-notice }

A local portal checks once a day whether a newer release exists, and shows a banner with the version,
a link to its release notes and the command to run, `whygraph update`. It checks when it starts, then
every 24 hours (6 hours after a failed check). A failed check never shows a banner; Settings says
"Last check failed".

- **A release is announced an hour after it is published,** because its image is built after the
  release appears.
- **The banner changes when a release needs manual steps.** It then points at these notes and this page,
  and `whygraph update` asks for `--force`. A portal that is many releases behind cannot verify the
  releases in between, and says the same.
- **Switch it off in Settings.** The toggle is under the portal's global Settings. Turning it off and
  on again makes a check due within 15 minutes. `whygraph update --check` answers on demand.
- **Production** shows the notice to instance admins only, as an Updates card on the Admin page; see
  [Upgrading](../deploy/production.md#upgrading).

What the check sends, and the other ways to turn it off, are in
[The update check](security.md#update-check).

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

2.0.0 replaces per-repo setup with the [portal](index.md). What changes is how you start WhyGraph,
where configuration and keys live, and how your agents connect. The portal does **not** reuse a
`.whygraph/whygraph.db` that 1.x left in a repository: it keeps each project's history in its own data
directory and never reads the in-repo file, so a project starts with empty history. The repository's
`.codegraph/` index is rebuilt by the first scan.

### Breaking changes

| 1.x | 2.0.0 |
|---|---|
| `whygraph init` | Removed. Add the repo in the portal; it initializes it. |
| `whygraph serve` | Removed. The portal is the web panel, for all your projects at once. |
| `whygraph analyze` | Removed. Per-commit descriptions come from a Full rescan, **Describe core** and **Describe all**. |
| `whygraph-mcp` (stdio server) | Removed. Agents connect over HTTP to `/mcp/<slug>` on the portal. The `whygraph-mcp` command remains only to print a message. |
| `whygraph scan` | Removed. The portal runs every scan (**Scan now**, the git hooks); there is no headless scan. |
| Hooks ran a local scan | Hooks ask the running portal to scan. They need the portal running; commits made while it was down are caught up when it next starts. |
| API keys and GitHub tokens from your shell environment | **No longer reach WhyGraph** in the portal. Enter them under Settings. |

`whygraph init`, `whygraph serve`, `whygraph scan` and `whygraph-mcp` still exist as stubs: they print a pointer to the
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

    - an existing `.whygraph/whygraph.db` is ignored (it is left in place, never read or migrated); a
      database the portal creates and later migrates is backed up in
      `projects/<org>/<slug>/backups/` in the data directory;
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

`whygraph scan` is gone, so nothing reads those variables any more; see
[Configuration](../reference/configuration.md#environment-variables).

### Custom database paths

The portal always uses `<repo>/.codegraph/codegraph.db` for the CodeGraph index, and keeps the WhyGraph
database in its data directory. If a 1.x `whygraph.toml` set `codegraph_db` (or the old `whygraph_db`)
to somewhere else, the portal will not use it and says so when you add the project.

### Configuration keys

1.x keys still work through 2.x with a deprecation warning and are removed in 3.0; see
[Deprecated keys](../reference/configuration.md#deprecated-keys). A 1.x `whygraph.toml` resolves to the
same providers, models and timeouts it did before.

### Going back to 1.x

Your 1.x `.whygraph/whygraph.db` is still in your repository, because the portal never touches it.
To use a repository without the portal again, remove it from the Projects page (or delete
`.whygraph/portal.json`), then install 1.x again.
