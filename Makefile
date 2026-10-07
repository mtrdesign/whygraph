# WhyGraph dev tasks. Run `make` (or `make help`) to list targets.
#
#   make dev-local    develop natively  (portal :8777 + Vite HMR :5174, auto-restart)
#   make dev-production  the same in production mode (whygraph.localhost:5173; runs beside dev-local)
#   make dev-docker   develop in Docker (the same, inside the image via the real shim)
#   make prod         the image exactly as released, through the shim (:8777)
#   make check        everything CI runs, plus e2e and the release smoke test
#
# Every mode is isolated from your own portal (`whygraph up`): its own
# container name, data dir and HOME under $(DEV), and it shares only the
# scratch repos in $(DEV)/repos - never this checkout.

.DEFAULT_GOAL := help

# `uv run` syncs first, which fails behind a TLS-intercepting proxy; use
# `UV_NO_SYNC=1 make ...` or `make ... UV_RUN="uv run --no-sync"` there.
UV_RUN ?= uv run

# Scratch state for all dev modes (canonical path: Docker shares it at the same path).
DEV ?= $(shell cd "$${TMPDIR:-/tmp}" && pwd -P)/whygraph-dev
DEV_PORT ?= 8777
DEV_PROD_PORT ?= 8778
DEV_GITHUB_PORT ?= 18767

# `make dev-db`: the dev portal's Postgres, laid out like the shim's
# (`whygraph up`) but published on 127.0.0.1 with a dev-only password.
PG_DEV_PORT ?= 55432
PG_DEV_NAME := whygraph-dev-postgres
DEV_DATABASE_URL = postgresql+psycopg://whygraph:whygraph-dev@127.0.0.1:$(PG_DEV_PORT)/whygraph
DEV_PROD_DATABASE_URL = postgresql+psycopg://whygraph:whygraph-dev@127.0.0.1:$(PG_DEV_PORT)/whygraph_prod

# `make dev-production`'s GitHub: a real one only when every OAuth App and
# GitHub App variable is set (in $(DEV_ENV_FILE) or the environment), the fake
# (tests/github_fake.py) for both when none is; a partial set stops before
# anything starts. DEV_GITHUB_MODE loads the file and sets `github=real|fake`.
DEV_ENV_FILE ?= ./.env.dev
DEV_GITHUB_VARS := WHYGRAPH_GITHUB_OAUTH_CLIENT_ID WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE \
	WHYGRAPH_GITHUB_APP_SLUG WHYGRAPH_GITHUB_APP_CLIENT_ID WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE \
	WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE
DEV_GITHUB_MODE = if [ -f "$(DEV_ENV_FILE)" ]; then echo "loading $(DEV_ENV_FILE)"; set -a; . "$(DEV_ENV_FILE)"; set +a; fi; \
	present=; missing=; \
	for v in $(DEV_GITHUB_VARS); do \
		if [ -n "$$(printenv $$v)" ]; then present="$$present $$v"; else missing="$$missing $$v"; fi; \
	done; \
	if [ -z "$$present" ]; then github=fake; \
	elif [ -z "$$missing" ]; then github=real; \
	else echo "error: a real GitHub needs every OAuth App and GitHub App variable (see .env.dev.example); missing:$$missing" >&2; exit 1; fi

# `make inspect SLUG=<slug>` points the MCP Inspector at one project's MCP
# endpoint on the running dev portal.
SLUG ?= whygraph

# Local image tag. `make image` builds it with the release's build args.
IMAGE ?= whygraph:dev
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
CODEGRAPH_VERSION := $(shell sed -n "s/^ *CODEGRAPH_VERSION: *'\([^']*\)'.*/\1/p" .github/workflows/cd-deploy-whygraph.yml | head -1)
# What the image is built from besides src/: a change here means `dev-docker` rebuilds.
DEPS_HASH = $(shell cat pyproject.toml uv.lock hatch_build.py docker/whygraph/Dockerfile src/playground/package-lock.json | git hash-object --stdin | cut -c1-16)

.PHONY: help dev dev-local dev-production dev-docker prod check test e2e docs docs-build db db-down dev-db dev-db-down inspect image sync playground node-check playground-deps dev-fixtures dev-image dev-github-check

help:  ## List available targets
	@echo "Run WhyGraph:"
	@grep -hE '^(dev-local|dev-production|dev-docker|prod|check):.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-15s %s\n", $$1, $$2}'
	@echo "Helpers:"
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | grep -vE '^(help|dev-local|dev-production|dev-docker|prod|check):' | sort | awk 'BEGIN{FS=":.*?## "}{printf "  %-15s %s\n", $$1, $$2}'

# --- run -------------------------------------------------------------------

dev:
	@echo "pick one: 'make dev-local' (native, fastest) or 'make dev-docker' (inside the image)"; exit 2

dev-local: node-check dev-fixtures dev-db  ## Develop natively: portal :8777 (auto-restart) + Vite HMR :5174 - open :5174
	WHYGRAPH_DATABASE_URL="$(DEV_DATABASE_URL)" \
	WHYGRAPH_SHARED_FOLDERS="$(DEV)/repos" WHYGRAPH_DEV_ORIGINS=http://localhost:5174,http://127.0.0.1:5174 \
	WHYGRAPH_DEV_PLATFORM_HTTP=1 NO_PROXY=.localhost \
		$(UV_RUN) python scripts/dev_portal.py -- --data "$(DEV)/local/data" --port $(DEV_PORT)

dev-production: dev-github-check node-check dev-fixtures dev-db  ## Develop in production mode: portal :8778 + Vite :5173 on whygraph.localhost:5173 (runs beside dev-local)
	@docker exec $(PG_DEV_NAME) psql -U whygraph -d whygraph -tAc "SELECT 1 FROM pg_database WHERE datname='whygraph_prod'" | grep -q 1 \
		|| docker exec $(PG_DEV_NAME) createdb -U whygraph whygraph_prod
	@mkdir -p "$(DEV)/production/data"
	@echo "open http://whygraph.localhost:5173 (Chromium or Firefox); the bootstrap secret is printed below"
	@set -e; $(DEV_GITHUB_MODE); \
	if [ "$$github" = fake ]; then \
		gh="$(DEV)/production/github"; mkdir -p "$$gh"; \
		( umask 077; \
		  [ -s "$$gh/oauth-secret" ] || printf 'dev-client-secret\n' > "$$gh/oauth-secret"; \
		  [ -s "$$gh/app-secret" ] || printf 'dev-app-client-secret\n' > "$$gh/app-secret"; \
		  [ -s "$$gh/webhook-secret" ] || openssl rand -hex 24 > "$$gh/webhook-secret"; \
		  [ -s "$$gh/app-key.pem" ] || openssl genrsa -out "$$gh/app-key.pem" 2048 2>/dev/null ); \
		echo "GitHub: the fake on 127.0.0.1:$(DEV_GITHUB_PORT) for both apps, fixture repos in $$gh/repos (see .env.dev.example to use a real GitHub)"; \
		$(UV_RUN) python tests/github_fake.py --host 127.0.0.1 --port $(DEV_GITHUB_PORT) \
			--client-id dev-client --client-secret-file "$$gh/oauth-secret" \
			--redirect-uri http://whygraph.localhost:5173/auth/github \
			--app-client-id dev-app --app-client-secret-file "$$gh/app-secret" \
			--app-callback http://whygraph.localhost:5173/auth/github-app \
			--app-key-file "$$gh/app-key.pem" --app-slug whygraph-dev \
			--webhook-secret-file "$$gh/webhook-secret" \
			--webhook-url http://127.0.0.1:$(DEV_PROD_PORT)/github/webhook --webhook-host whygraph.localhost:5173 \
			--repos "$$gh/repos" & \
		fake=$$!; \
		trap 'kill $$fake 2>/dev/null || true' EXIT INT TERM; \
		export WHYGRAPH_GITHUB_OAUTH_CLIENT_ID=dev-client \
			WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE="$$gh/oauth-secret" \
			WHYGRAPH_GITHUB_APP_SLUG=whygraph-dev WHYGRAPH_GITHUB_APP_CLIENT_ID=dev-app \
			WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE="$$gh/app-secret" \
			WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE="$$gh/app-key.pem" \
			WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE="$$gh/webhook-secret" \
			WHYGRAPH_GITHUB_URL=http://127.0.0.1:$(DEV_GITHUB_PORT) \
			WHYGRAPH_GITHUB_API_URL=http://127.0.0.1:$(DEV_GITHUB_PORT)/api/v3; \
	else \
		echo "GitHub: real (both apps from $(DEV_ENV_FILE) or the environment); relay its webhooks with:"; \
		echo "  npx smee-client -u <channel> -t http://whygraph.localhost:5173/github/webhook"; \
	fi; \
	env -u WHYGRAPH_SHARED_FOLDERS -u WHYGRAPH_DEV_ORIGINS \
	WHYGRAPH_MODE=production WHYGRAPH_BASE_URL=http://whygraph.localhost:5173 \
	WHYGRAPH_DATABASE_URL="$(DEV_PROD_DATABASE_URL)" \
		$(UV_RUN) python scripts/dev_portal.py --preserve-host --vite-port 5173 -- --data "$(DEV)/production/data" --port $(DEV_PROD_PORT)

dev-docker: dev-fixtures dev-image  ## Develop in Docker: the same, inside the image via the real shim - open :5174
	sh scripts/dev_docker.sh dev "$(IMAGE)" "$(DEV)" $(DEV_PORT)

prod: dev-fixtures image  ## The image exactly as released, through the shim - open :8777
	sh scripts/dev_docker.sh prod "$(IMAGE)" "$(DEV)" $(DEV_PORT)

check: node-check  ## Before a PR: lint, pytest, frontend, e2e, then the smoke test on a fresh image
	$(UV_RUN) ruff check src/ tests/ scripts/
	$(UV_RUN) ruff format --check src/ tests/ scripts/
	$(UV_RUN) pytest -q
	npm --prefix src/playground ci
	npm --prefix src/playground run typecheck
	npm --prefix src/playground test
	npm --prefix src/playground run build
	sh src/playground/e2e/run.sh
	$(MAKE) image
	sh scripts/smoke.sh "$(IMAGE)"

# --- helpers ---------------------------------------------------------------

sync:  ## Install / refresh the uv environment
	uv sync

test:  ## Run the Python test suite
	$(UV_RUN) pytest

e2e: playground  ## Playwright suite vs a throwaway portal + fake scanner (extra args: ARGS="--project=light")
	sh src/playground/e2e/run.sh $(ARGS)

image:  ## Build the image like the release does (pinned CodeGraph, real version); IMAGE=... to retag
	docker build -f docker/whygraph/Dockerfile \
		--build-arg CODEGRAPH_VERSION=$(CODEGRAPH_VERSION) \
		--build-arg WHYGRAPH_VERSION=$(VERSION) \
		--label whygraph.deps=$(DEPS_HASH) \
		-t $(IMAGE) .

playground: node-check  ## Production build of the SPA into src/whygraph/serve/static
	npm --prefix src/playground ci
	npm --prefix src/playground run build

docs:  ## Serve the docs site locally with live reload (social cards skipped - no Cairo needed)
	$(UV_RUN) mkdocs serve

docs-build:  ## Build the static docs site into ./site (strict; cards skipped unless CI=true)
	$(UV_RUN) mkdocs build --strict

inspect:  ## MCP Inspector vs a project's endpoint on the running dev portal (SLUG=<slug>)
	@node -e 'process.exit(+process.versions.node.split(".")[0]>=20?0:1)' 2>/dev/null || { echo "error: MCP Inspector needs Node >= 20 (have $$(node -v 2>/dev/null || echo none)) - try 'nvm use 22'"; exit 1; }
	npx @modelcontextprotocol/inspector --transport http --server-url http://127.0.0.1:$(DEV_PORT)/mcp/$(SLUG)

db:  ## DBGate viewer for a scratch repo's two databases and the dev portal DB (http://localhost:8081; SLUG=<repo>)
	@test -f docker-compose.yml || { echo "error: docker-compose.yml missing - run: cp docker-compose.example.yml docker-compose.yml"; exit 1; }
	@test -f "$(DEV)/repos/$(SLUG)/.whygraph/whygraph.db" || echo "warning: $(DEV)/repos/$(SLUG)/.whygraph/whygraph.db missing - add and scan the project in a dev portal"
	DB_REPO="$(DEV)/repos/$(SLUG)" docker compose up -d
	@echo "DBGate -> http://localhost:8081  (WhyGraph + CodeGraph, and Portal while make dev-db runs)"

db-down:  ## Stop the DBGate database viewer
	docker compose down

# Same layout as the shim's database container: the host dir mounted at
# /var/lib/postgresql (never .../data), a per-major PGDATA, the host user, and
# the TCP health check (the image's first-run temporary server is socket-only).
# The pin comes from the Python constant the shim bakes, so the two cannot drift.
dev-db:  ## Start the dev portal's Postgres (whygraph-dev-postgres on 127.0.0.1, PG_DEV_PORT=55432) and wait until healthy
	@state=$$(docker inspect -f '{{.State.Status}}' $(PG_DEV_NAME) 2>/dev/null || true); \
	if [ "$$state" = running ]; then :; \
	elif [ -n "$$state" ]; then docker start $(PG_DEV_NAME) >/dev/null; \
	else \
		set -- $$(uv run --no-sync python -c 'from whygraph.cli.commands.install import POSTGRES_IMAGE, POSTGRES_MAJOR; print(POSTGRES_IMAGE, POSTGRES_MAJOR)'); \
		[ $$# -eq 2 ] || { echo "error: cannot read POSTGRES_IMAGE from the checkout - run 'uv sync'"; exit 1; }; \
		mkdir -p "$(DEV)/local/postgres" && chmod 700 "$(DEV)/local/postgres" || exit 1; \
		echo "starting $(PG_DEV_NAME) ($$1) on 127.0.0.1:$(PG_DEV_PORT)"; \
		docker run -d --name $(PG_DEV_NAME) \
			--user "$$(id -u):$$(id -g)" \
			--shm-size=128m \
			--health-cmd "pg_isready -h 127.0.0.1 -U whygraph -d whygraph" \
			--health-interval 2s --health-timeout 3s --health-retries 30 --health-start-period 5s \
			--mount "type=bind,source=$(DEV)/local/postgres,target=/var/lib/postgresql" \
			-e "PGDATA=/var/lib/postgresql/$$2/docker" \
			-e POSTGRES_USER=whygraph -e POSTGRES_DB=whygraph -e POSTGRES_PASSWORD=whygraph-dev \
			-p "127.0.0.1:$(PG_DEV_PORT):5432" \
			"$$1" >/dev/null || exit 1; \
	fi; \
	i=0; \
	while :; do \
		health=$$(docker inspect -f '{{.State.Health.Status}}' $(PG_DEV_NAME) 2>/dev/null || true); \
		[ "$$health" != healthy ] || break; \
		if [ "$$health" = unhealthy ] || [ $$i -ge 60 ]; then echo "error: $(PG_DEV_NAME) did not become ready - see: docker logs $(PG_DEV_NAME)"; exit 1; fi; \
		i=$$((i + 1)); sleep 1; \
	done; \
	echo "$(PG_DEV_NAME) ready: $(DEV_DATABASE_URL)"

dev-db-down:  ## Stop and remove the dev Postgres (its data stays under $TMPDIR/whygraph-dev/local)
	@if docker inspect $(PG_DEV_NAME) >/dev/null 2>&1; then \
		docker stop -t 30 $(PG_DEV_NAME) >/dev/null && docker rm $(PG_DEV_NAME) >/dev/null && echo "$(PG_DEV_NAME) stopped (data stays in $(DEV)/local/postgres)"; \
	else echo "$(PG_DEV_NAME) is not running"; fi

# --- internal --------------------------------------------------------------

dev-github-check:  # dev-production's GitHub: stop on a partial set of variables before anything starts
	@$(DEV_GITHUB_MODE); echo "GitHub for dev-production: $$github"

node-check:  # assert Node >= 22.12 for the playground toolchain (Vite 8 / Vitest 5)
	@node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=12)?0:1)' 2>/dev/null || { echo "error: the playground needs Node >= 22.12 (have $$(node -v 2>/dev/null || echo none)) - try 'nvm use 22'"; exit 1; }

playground-deps: node-check  # install node_modules only if missing
	@[ -d src/playground/node_modules ] || npm --prefix src/playground ci

dev-fixtures:  # the scratch repos every mode shares (offline, idempotent)
	@sh scripts/dev_fixtures.sh "$(DEV)/repos"

dev-image:  # (re)build $(IMAGE) only when its dependency inputs changed
	@[ "$$(docker image inspect -f '{{index .Config.Labels "whygraph.deps"}}' $(IMAGE) 2>/dev/null)" = "$(DEPS_HASH)" ] \
		|| { echo "building $(IMAGE) (dependencies changed or no image yet)"; $(MAKE) image; }
