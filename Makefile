# WhyGraph dev tasks. Run `make` (or `make help`) to list targets.
#
#   make dev-local    develop natively  (portal :8777 + Vite HMR :5173, auto-restart)
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

# `make inspect SLUG=<slug>` points the MCP Inspector at one project's MCP
# endpoint on the running dev portal.
SLUG ?= whygraph

# Local image tag. `make image` builds it with the release's build args.
IMAGE ?= whygraph:dev
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
CODEGRAPH_VERSION := $(shell sed -n "s/^ *CODEGRAPH_VERSION: *'\([^']*\)'.*/\1/p" .github/workflows/cd-deploy-whygraph.yml | head -1)
CLAUDE_CODE_VERSION := $(shell sed -n "s/^ *CLAUDE_CODE_VERSION: *'\([^']*\)'.*/\1/p" .github/workflows/cd-deploy-whygraph.yml | head -1)
# What the image is built from besides src/: a change here means `dev-docker` rebuilds.
DEPS_HASH = $(shell cat pyproject.toml uv.lock hatch_build.py docker/whygraph/Dockerfile src/playground/package-lock.json | git hash-object --stdin | cut -c1-16)

.PHONY: help dev dev-local dev-docker prod check test e2e docs docs-build db db-down inspect image sync playground node-check playground-deps dev-fixtures dev-image

help:  ## List available targets
	@echo "Run WhyGraph:"
	@grep -hE '^(dev-local|dev-docker|prod|check):.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-11s %s\n", $$1, $$2}'
	@echo "Helpers:"
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | grep -vE '^(help|dev-local|dev-docker|prod|check):' | sort | awk 'BEGIN{FS=":.*?## "}{printf "  %-11s %s\n", $$1, $$2}'

# --- run -------------------------------------------------------------------

dev:
	@echo "pick one: 'make dev-local' (native, fastest) or 'make dev-docker' (inside the image)"; exit 2

dev-local: node-check dev-fixtures  ## Develop natively: portal :8777 (auto-restart) + Vite HMR :5173 - open :5173
	WHYGRAPH_SHARED_FOLDERS="$(DEV)/repos" WHYGRAPH_DEV_ORIGINS=http://localhost:5173,http://127.0.0.1:5173 \
		$(UV_RUN) python scripts/dev_portal.py -- --data "$(DEV)/local/data" --port $(DEV_PORT)

dev-docker: dev-fixtures dev-image  ## Develop in Docker: the same, inside the image via the real shim - open :5173
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
		--build-arg CLAUDE_CODE_VERSION=$(CLAUDE_CODE_VERSION) \
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

db:  ## DBGate viewer for a scratch repo's two databases (http://localhost:8081; SLUG=<repo>)
	@test -f docker-compose.yml || { echo "error: docker-compose.yml missing - run: cp docker-compose.example.yml docker-compose.yml"; exit 1; }
	@test -f "$(DEV)/repos/$(SLUG)/.whygraph/whygraph.db" || echo "warning: $(DEV)/repos/$(SLUG)/.whygraph/whygraph.db missing - add and scan the project in a dev portal"
	DB_REPO="$(DEV)/repos/$(SLUG)" docker compose up -d
	@echo "DBGate -> http://localhost:8081  (WhyGraph + CodeGraph in the sidebar)"

db-down:  ## Stop the DBGate database viewer
	docker compose down

# --- internal --------------------------------------------------------------

node-check:  # assert Node >= 22.12 for the playground toolchain (Vite 8 / Vitest 5)
	@node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=12)?0:1)' 2>/dev/null || { echo "error: the playground needs Node >= 22.12 (have $$(node -v 2>/dev/null || echo none)) - try 'nvm use 22'"; exit 1; }

playground-deps: node-check  # install node_modules only if missing
	@[ -d src/playground/node_modules ] || npm --prefix src/playground ci

dev-fixtures:  # the scratch repos every mode shares (offline, idempotent)
	@sh scripts/dev_fixtures.sh "$(DEV)/repos"

dev-image:  # (re)build $(IMAGE) only when its dependency inputs changed
	@[ "$$(docker image inspect -f '{{index .Config.Labels "whygraph.deps"}}' $(IMAGE) 2>/dev/null)" = "$(DEPS_HASH)" ] \
		|| { echo "building $(IMAGE) (dependencies changed or no image yet)"; $(MAKE) image; }
