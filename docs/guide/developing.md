# Developing WhyGraph

Working on WhyGraph itself (not using it) takes five `make` targets from a checkout:

| Command | Mode | What it runs |
|---|---|---|
| `make dev-local` | Development, native | The portal from your checkout on `:8777`, restarted on every backend change, plus the Vite dev server with hot reload on `:5173`, against the dev Postgres from `make dev-db` (started for you). Fastest; your IDE's debugger works. |
| `make dev-docker` | Development, in Docker | The same, inside the WhyGraph image through the real `whygraph up` shim, with your checkout mounted read-only over the installed package. |
| `make prod` | Production | The image built exactly like a release (pinned CodeGraph, the `pyproject.toml` version), run through the shim with no source mounted: what users get. |
| `make dev-production` | Development, native, production mode | The portal's [production mode](../deploy/production.md) from your checkout on `:8778`, with Vite on `:5173` in front of it, on its own `whygraph_prod` database in the `make dev-db` Postgres. For work on sign-in, organizations and the host checks. See below. |
| `make check` | Before a pull request | Both `ruff` checks, `pytest`, the frontend typecheck / tests / build, the Playwright suite, then the release smoke test against a freshly built image. Stops at the first failure. |

`dev-local` and `dev-docker` run in the foreground: open `http://localhost:5173`, and press
Ctrl-C to stop everything. `prod` serves the built UI itself on `http://127.0.0.1:8777`.

## The portal database in development

The portal keeps its own data in Postgres, so every mode needs one:

- **`make dev-local`** depends on **`make dev-db`**, which starts (or reuses) a container,
  `whygraph-dev-postgres`, laid out like the shim's database container (the same pinned image, read
  from `POSTGRES_IMAGE` in `cli/commands/install.py`, a per-major `PGDATA`, your user, the same health
  check) but published on `127.0.0.1:55432` (`PG_DEV_PORT=...` to change it) with the dev-only
  password `whygraph-dev`. Its files are in `$TMPDIR/whygraph-dev/local/postgres`, beside the dev data
  directory. `make dev-db` waits until it is healthy; `make dev-local` then points the portal at it
  with `WHYGRAPH_DATABASE_URL=postgresql+psycopg://whygraph:whygraph-dev@127.0.0.1:55432/whygraph`.
  `make dev-db-down` stops and removes the container and keeps its files; delete the directory for a
  fresh database.
- **`make dev-docker`** and **`make prod`** go through the real shim, so they get their own database
  container, `whygraph-portal-dev-postgres` / `whygraph-portal-prod-postgres`, which their Ctrl-C
  `whygraph down` stops with the portal.
- **`make e2e`** starts a throwaway Postgres (in memory) for its portal and removes it afterwards.

`make dev-db` and `make dev-db-down` are listed by `make` like every other helper.

## Production mode in development

`make dev-production` runs the same native stack as `dev-local` with `WHYGRAPH_MODE=production`. It
differs from `make prod`, which is the *released image in local mode* (what a user installs), not
production mode:

- It uses a separate database, `whygraph_prod`, created on demand in the `make dev-db` container, and
  its own data directory under `$TMPDIR/whygraph-dev/production`, so it never touches `dev-local`'s
  state. No shared folders are given.
- The base URL is `http://whygraph.localhost:5173` - the **Vite** address, so the browser, the session
  cookie and the portal's host check all see one origin per host. Vite passes the `Host` header
  through unchanged, which is what lets `<org>.whygraph.localhost:5173` reach the portal. Open
  `http://whygraph.localhost:5173`, not `localhost`: that one gets `421`.
- The first run prints a `Bootstrap secret:` line in the terminal (the portal's log); the page at
  `http://whygraph.localhost:5173` asks for it to create the first account.
- Under the hood `make dev-production` runs `scripts/dev_portal.py --preserve-host`: Vite then
  forwards `Host` unchanged and only answers `*.whygraph.localhost`, and the script prints the
  `whygraph.localhost` URL (the bare `127.0.0.1:8778` address is refused with `421` in production).
- It cannot run beside `dev-local`: both keep Vite on `:5173`.
- Use **Chromium or Firefox**: they resolve every `*.localhost` name to the loopback address, while
  Safari's handling of `*.localhost` varies by version.

## Which one to use

- **`dev-local`** for most work: the UI, routes, MCP tools, chat. A saved `.py` file under
  `src/whygraph/` restarts the portal within a few seconds (`RELOAD=0` turns that off); a saved
  `.tsx` file hot-reloads in the browser.
- **`dev-docker`** for anything that behaves differently in the container: the shim
  (`cli/commands/install.py`), the Dockerfile, file ownership, paths, the environment a scan gets,
  and the in-image `git` / `gh` / `codegraph`. It rebuilds the image only when its inputs change
  (`pyproject.toml`, `uv.lock`, the Dockerfile, `package-lock.json`); everyday edits reach the
  container through the mount.
- **`dev-production`** for anything that only exists in production mode: accounts, sessions,
  organization hosts, the admin page.
- **`make check`** always, before pushing. Its smoke test catches what `dev-local` cannot see.

## What stays isolated

Every mode keeps to itself, so none of them touches your own portal:

- its own container name (`whygraph-portal-dev`, `whygraph-portal-prod`, and
  `whygraph-portal-smoke` for the smoke test), data directory and `HOME`, all under
  `$TMPDIR/whygraph-dev`;
- one scratch folder of repos, `$TMPDIR/whygraph-dev/repos`, shared by every mode and created
  offline on first use: a copy of your checkout (`git clone --no-hardlinks`, real history to
  dogfood) and a tiny three-commit demo repo. Delete one to get a fresh copy next time.

Your checkout itself is never added to a dev portal, so it never gets WhyGraph's hooks or markers
and `whygraph scan` keeps working in it.

The three run modes share port `8777`, so run one at a time. Override it with `DEV_PORT=...`.
`dev-production` uses `:8778` for the portal and `:5173` for Vite, so it cannot run beside `dev-local`.

## Helpers

| Command | What it does |
|---|---|
| `make test` | `pytest` only |
| `make e2e` | The Playwright suite against a throwaway portal and a fake scanner, both themes (`ARGS="--project=light"` for one) |
| `make inspect SLUG=<slug>` | The MCP Inspector against a project's `/mcp/<slug>` on the running dev portal |
| `make db SLUG=<slug>` | A DBGate viewer on `:8081` for a scratch repo's two databases, plus a **Portal** connection to the `make dev-db` Postgres (through `host.docker.internal:55432`). Copy `docker-compose.example.yml` to `docker-compose.yml` first; an older copy needs refreshing to get the Portal connection |
| `make dev-db` / `make dev-db-down` | Start (and wait for) / stop the dev portal's Postgres; see above |
| `make image` | Build the image like the release does (`IMAGE=...` to retag) |
| `make docs` / `make docs-build` | Serve this site with live reload / build it strictly, as CI does |

## Requirements and gotchas

- **Node 22.12 or newer** for the playground toolchain (`nvm use 22`).
- **Docker** for `dev-local` (its Postgres), `dev-docker`, `prod`, `make e2e` and `make check`.
- **The tests need a Postgres.** The portal tests run against a real one: by default `pytest` starts a
  throwaway container of the pinned image for the session (on a random loopback port, in memory) and
  gives every test its own database, so plain `uv run pytest` needs Docker. Without Docker, set
  `WHYGRAPH_TEST_DATABASE_URL` to an **admin** URL of a Postgres you run (the role needs `CREATEDB`),
  for example `postgresql+psycopg://postgres:test@127.0.0.1:5432/postgres`; CI does exactly that with
  a service container. `WHYGRAPH_TEST_POSTGRES_IMAGE` replaces the throwaway server's image, for a
  registry mirror. With neither Docker nor the URL, the portal tests **fail** with a message saying
  so - they are never skipped.
- **`uv run` syncs first**, which fails behind a TLS-intercepting proxy. Run the targets with
  `UV_NO_SYNC=1 make ...` (or `UV_RUN="uv run --no-sync"`) there.
- The Docker modes use two development-only shim settings, `WHYGRAPH_DEV_SRC` and
  `WHYGRAPH_PORTAL_NAME`. They are not part of the supported interface.
