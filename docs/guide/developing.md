# Developing WhyGraph

Working on WhyGraph itself (not using it) takes four `make` targets from a checkout:

| Command | Mode | What it runs |
|---|---|---|
| `make dev-local` | Development, native | The portal from your checkout on `:8777`, restarted on every backend change, plus the Vite dev server with hot reload on `:5173`. Fastest; your IDE's debugger works. |
| `make dev-docker` | Development, in Docker | The same, inside the WhyGraph image through the real `whygraph up` shim, with your checkout mounted read-only over the installed package. |
| `make prod` | Production | The image built exactly like a release (pinned CodeGraph, the `pyproject.toml` version), run through the shim with no source mounted: what users get. |
| `make check` | Before a pull request | Both `ruff` checks, `pytest`, the frontend typecheck / tests / build, the Playwright suite, then the release smoke test against a freshly built image. Stops at the first failure. |

`dev-local` and `dev-docker` run in the foreground: open `http://localhost:5173`, and press
Ctrl-C to stop everything. `prod` serves the built UI itself on `http://127.0.0.1:8777`.

## Which one to use

- **`dev-local`** for most work: the UI, routes, MCP tools, chat. A saved `.py` file under
  `src/whygraph/` restarts the portal within a few seconds (`RELOAD=0` turns that off); a saved
  `.tsx` file hot-reloads in the browser.
- **`dev-docker`** for anything that behaves differently in the container: the shim
  (`cli/commands/install.py`), the Dockerfile, file ownership, paths, the environment a scan gets,
  and the in-image `git` / `gh` / `codegraph`. It rebuilds the image only when its inputs change
  (`pyproject.toml`, `uv.lock`, the Dockerfile, `package-lock.json`); everyday edits reach the
  container through the mount.
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

## Helpers

| Command | What it does |
|---|---|
| `make test` | `pytest` only |
| `make e2e` | The Playwright suite against a throwaway portal and a fake scanner, both themes (`ARGS="--project=light"` for one) |
| `make inspect SLUG=<slug>` | The MCP Inspector against a project's `/mcp/<slug>` on the running dev portal |
| `make db SLUG=<slug>` | A DBGate viewer for a scratch repo's two databases on `:8081` (copy `docker-compose.example.yml` to `docker-compose.yml` first) |
| `make image` | Build the image like the release does (`IMAGE=...` to retag) |
| `make docs` / `make docs-build` | Serve this site with live reload / build it strictly, as CI does |

## Requirements and gotchas

- **Node 22.12 or newer** for the playground toolchain (`nvm use 22`).
- **Docker** for `dev-docker`, `prod` and `make check`.
- **`uv run` syncs first**, which fails behind a TLS-intercepting proxy. Run the targets with
  `UV_NO_SYNC=1 make ...` (or `UV_RUN="uv run --no-sync"`) there.
- The Docker modes use two development-only shim settings, `WHYGRAPH_DEV_SRC` and
  `WHYGRAPH_PORTAL_NAME`. They are not part of the supported interface.
