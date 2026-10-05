# Developing WhyGraph

Working on WhyGraph itself (not using it) takes five `make` targets from a checkout:

| Command | Mode | What it runs |
|---|---|---|
| `make dev-local` | Development, native | The portal from your checkout on `:8777`, restarted on every backend change, plus the Vite dev server with hot reload on `:5174`, against the dev Postgres from `make dev-db` (started for you). Fastest; your IDE's debugger works. |
| `make dev-docker` | Development, in Docker | The same, inside the WhyGraph image through the real `whygraph up` shim, with your checkout mounted read-only over the installed package. |
| `make prod` | Production | The image built exactly like a release (pinned CodeGraph, the `pyproject.toml` version), run through the shim with no source mounted: what users get. |
| `make dev-production` | Development, native, production mode | The portal's [production mode](../deploy/production.md) from your checkout on `:8778`, with Vite on `:5173` in front of it, on its own `whygraph_prod` database in the `make dev-db` Postgres. For work on sign-in, organizations and the host checks, and the platform `dev-local` links to. See below. |
| `make check` | Before a pull request | Both `ruff` checks, `pytest`, the frontend typecheck / tests / build, the Playwright suite, then the release smoke test against a freshly built image. Stops at the first failure. |

`dev-local` and `dev-docker` run in the foreground: open `http://localhost:5174`, and press
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
- **GitHub is a fake.** Sign-in and projects need a GitHub, so `make dev-production` also starts a
  small fake one (`tests/github_fake.py`) on `DEV_GITHUB_PORT` that serves **both apps**: the OAuth
  App and the GitHub App. It points every `WHYGRAPH_GITHUB_*` variable at it, with a generated app
  key, client secrets and webhook secret kept under `$TMPDIR/whygraph-dev/production/github/`
  (generating them needs `openssl`). Nothing leaves your machine. Its sign-in page offers four users,
  `ben`, `cy`, `dee` and `nofa` (no two-factor authentication), to try members, roles and two-factor
  refusals.
- **The fake's repositories.** It serves two fixture repositories, `ben/demo` and `ben/notes`, as bare
  repositories in `$TMPDIR/whygraph-dev/production/github/repos`, covered by installation `100` on
  Ben's account: sign in as `ben` to import them. Its control routes change its state and send the
  matching signed webhook to the portal, so you can try the webhook paths without a real GitHub:

    ```bash
    curl -X POST http://127.0.0.1:18767/_fake/push -d '{"repo": "ben/demo"}'          # a commit + a push
    curl -X POST http://127.0.0.1:18767/_fake/uninstall -d '{"installation": 100}'    # access lost
    curl -X POST http://127.0.0.1:18767/_fake/remove-repo -d '{"repo": "ben/demo"}'   # access lost
    ```

- **A real GitHub** is used only when **every** OAuth App and GitHub App variable is set, because the
  two apps share `WHYGRAPH_GITHUB_URL` and a real one cannot be mixed with the fake; with none set the
  fake serves both, and a partial set stops `make dev-production` before anything starts, naming the
  missing variables. Create a dev OAuth App and a dev GitHub App with the callback URLs
  `http://whygraph.localhost:5173/auth/github` and `http://whygraph.localhost:5173/auth/github-app`
  (see [GitHub sign-in](../deploy/production.md#github-sign-in) and
  [The GitHub App](../deploy/production.md#the-github-app)), then copy `.env.dev.example` to
  `.env.dev` (gitignored) and fill in all seven variables. `make dev-production` loads `.env.dev` (or
  the file `DEV_ENV_FILE=...` names) when it exists; values in it win over your shell's.
- **Webhooks from a real GitHub** cannot reach `whygraph.localhost`, so relay them through a
  [smee.io](https://smee.io) channel: set the dev app's webhook URL to the channel, then run
  `npx smee-client -u https://smee.io/<channel> -t http://whygraph.localhost:5173/github/webhook`
  beside `make dev-production`. The relay goes through Vite, which proxies `/github` to the portal and
  keeps the base host it requires. Without the relay, pushes still arrive through the hourly check
  and **Scan now**.
- The first run prints a `Bootstrap secret:` line in the terminal (the portal's log); the page at
  `http://whygraph.localhost:5173` asks for it to create the first account.
- Under the hood `make dev-production` runs
  `scripts/dev_portal.py --preserve-host --vite-port 5173`: Vite then forwards `Host` unchanged and
  only answers `*.whygraph.localhost`, and the script prints the `whygraph.localhost` URL (the bare
  `127.0.0.1:8778` address is refused with `421` in production).
- It **runs beside `dev-local`**, which is what linking a checkout needs: the two differ in every
  port (`:8778` with Vite on `:5173` here, `:8777` with Vite on `:5174` there) and use different
  databases. See [Linking the two dev portals](#linking-the-two-dev-portals).
- Use **Chromium or Firefox**: they resolve every `*.localhost` name to the loopback address, while
  Safari's handling of `*.localhost` varies by version.

## Linking the two dev portals

A platform project is linked from a local portal, so trying one out means running both:
`make dev-production` (the platform) in one terminal and `make dev-local` (the local portal) in
another. They no longer collide - the local portal's Vite is on `:5174`, the platform's on `:5173`.

1. In `dev-production`, sign in to the fake GitHub as `ben`, import `ben/demo` and scan it.
2. Open the project's **Use with your agent** and enter your local portal's port. In the dev loop
   that is **5174** - Vite's port, not the portal's `8777`, because the browser talks to Vite (it
   serves the `/link` and `/connect/callback` pages and proxies `/api` to the portal).
3. The button lands on the local portal's `/link` page. Press **Connect**, allow it on the platform,
   and the browser comes back to `http://127.0.0.1:5174/connect/callback`.
4. Point the link at a checkout under `make dev-local`'s shared folder,
   `$TMPDIR/whygraph-dev/repos`, whose `origin` names the platform's repository (or which holds the
   commit the platform scanned). The fake GitHub serves git only to an installation token, so the
   quickest one is a clone of the platform's **own** server clone -
   `$TMPDIR/whygraph-dev/production/data/repos/<org>/<slug>` - with its `origin` set to
   `http://127.0.0.1:18767/ben/demo.git`.

`make dev-local` exports the two switches this needs, because a platform on
`http://whygraph.localhost:5173` is neither `https` nor a name every resolver maps to loopback:

- `WHYGRAPH_DEV_PLATFORM_HTTP=1` - accept an `http` platform, and only on a loopback host.
- `NO_PROXY=.localhost` - keep an `HTTP_PROXY` out of the portal's calls to the platform.

Neither is part of the supported interface; a real platform is always `https`.

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
  organization hosts, the admin page, projects imported from GitHub and the webhook.
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

`dev-local`, `dev-docker` and `prod` share port `8777`, so run one of those three at a time.
Override it with `DEV_PORT=...`. `dev-production` uses `:8778` for the portal and `:5173` for Vite,
so it can run beside any of them.

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
- **`openssl`** for `make e2e` and for `make dev-production` with the fake GitHub: both generate the
  fake GitHub App's key and webhook secret with it.
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
