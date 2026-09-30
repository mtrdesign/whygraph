# WhyGraph dev tasks. Run `make` (or `make help`) to list targets.

.DEFAULT_GOAL := help

# The dev portal (`make dev` / `make serve`): its data dir lives OUTSIDE the
# checkout (a data dir inside it would make the checkout un-addable), and the
# parent folder is shared so this checkout (and its siblings) can be added.
DEV_DATA ?= $${TMPDIR:-/tmp}/whygraph-dev
SHARED_FOLDERS ?= $(abspath ..)
PORTAL_PORT ?= 8765

# `make inspect SLUG=<slug>` points the MCP Inspector at one project's MCP
# endpoint on the running dev portal. Defaults to this checkout's slug.
SLUG ?= $(notdir $(CURDIR))

# Local tag for the dev image built by `make image`. Override to test an
# alternate tag, e.g. `make image IMAGE=whygraph:wip`.
IMAGE ?= whygraph:dev

# Name of the long-running container started by `make image-debug`.
DEBUG_NAME ?= whygraph-debug

.PHONY: help sync test scan node-check playground-deps playground playground-dev dev serve e2e docs docs-build db db-down inspect image image-test image-debug image-debug-down

help:  ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  %-10s %s\n", $$1, $$2}'

sync:  ## Install / refresh the uv environment
	uv sync

test:  ## Run the test suite
	uv run pytest

scan:  ## Re-scan this repo so WhyGraph is tested against itself (refuses once the checkout is portal-managed - scan from the dev portal then)
	uv run whygraph scan

node-check:  # (internal) assert Node >= 22.12 for the playground toolchain (Vite 8 / Vitest 5)
	@node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=12)?0:1)' 2>/dev/null || { echo "error: the playground needs Node >= 22.12 (have $$(node -v 2>/dev/null || echo none)) - try 'nvm use 22'"; exit 1; }

playground-deps: node-check  # (internal) install node_modules only if missing
	@[ -d src/playground/node_modules ] || npm --prefix src/playground ci

playground: node-check  ## Production build of the Explorer SPA into src/whygraph/serve/static
	npm --prefix src/playground ci
	npm --prefix src/playground run build

playground-dev: playground-deps  ## Vite dev server with HMR (:5173, proxies /api and /mcp to :8765) - pair with a backend or use 'make dev'
	npm --prefix src/playground run dev

dev: playground-deps  ## Dev loop: dev portal (:8765) + Vite HMR (:5173) together; open :5173; Ctrl-C stops both
	@echo "portal -> http://127.0.0.1:$(PORTAL_PORT)   playground (HMR) -> http://localhost:5173  (open :5173)"
	@echo "data dir: $(DEV_DATA)   shared folders: $(SHARED_FOLDERS)"
	@WHYGRAPH_SHARED_FOLDERS="$(SHARED_FOLDERS)" WHYGRAPH_DEV_ORIGINS=http://localhost:5173 \
	 uv run whygraph portal --data "$(DEV_DATA)" --port $(PORTAL_PORT) & \
	 api_pid=$$!; \
	 trap 'kill $$api_pid 2>/dev/null' EXIT INT TERM; \
	 npm --prefix src/playground run dev

serve: playground  ## Production preview: build the SPA then serve it from the dev portal (:8765)
	WHYGRAPH_SHARED_FOLDERS="$(SHARED_FOLDERS)" uv run whygraph portal --data "$(DEV_DATA)" --port $(PORTAL_PORT)

e2e: playground  ## Playwright suite vs a throwaway portal + fake scanner (local only, both themes; extra args: make e2e ARGS="--project=light")
	sh src/playground/e2e/run.sh $(ARGS)

docs:  ## Serve the docs site locally with live reload (social cards skipped — no Cairo needed)
	uv run mkdocs serve

docs-build:  ## Build the static docs site into ./site (strict; cards skipped unless CI=true)
	uv run mkdocs build --strict

db:  ## Start the DBGate database viewer (http://localhost:8081)
	@test -f docker-compose.yml || { echo "error: docker-compose.yml missing - run: cp docker-compose.example.yml docker-compose.yml"; exit 1; }
	@test -f .whygraph/whygraph.db || echo "warning: .whygraph/whygraph.db missing - add and scan the project in the dev portal"
	@test -f .codegraph/codegraph.db || echo "warning: .codegraph/codegraph.db missing - add and scan the project in the dev portal"
	docker compose up -d
	@echo "DBGate -> http://localhost:8081  (WhyGraph + CodeGraph in the sidebar)"

db-down:  ## Stop the DBGate database viewer
	docker compose down

inspect:  ## MCP Inspector vs a project's endpoint on the running dev portal (SLUG=<slug>)
	@node -e 'process.exit(+process.versions.node.split(".")[0]>=20?0:1)' 2>/dev/null || { echo "error: MCP Inspector needs Node >= 20 (have $$(node -v 2>/dev/null || echo none)) - try 'nvm use 22'"; exit 1; }
	npx @modelcontextprotocol/inspector --transport http --server-url http://127.0.0.1:$(PORTAL_PORT)/mcp/$(SLUG)

image:  ## Build the WhyGraph Docker image locally (override tag: IMAGE=...)
	docker build -f docker/whygraph/Dockerfile -t $(IMAGE) .

image-test: image  ## Build then smoke-test the image (CLI + bundled binaries)
	docker run --rm $(IMAGE) whygraph version
	docker run --rm $(IMAGE) sh -c 'for b in git gh node codegraph whygraph; do command -v "$$b" || { echo "missing: $$b" >&2; exit 1; }; done'
	@echo "image smoke test OK -> $(IMAGE)"

image-debug: image  ## Build, then run a detached container kept alive for `docker exec` debugging
	-docker rm -f $(DEBUG_NAME) 2>/dev/null || true
	docker run -d --name $(DEBUG_NAME) \
		-v "$(CURDIR):/workspace" -w /workspace \
		--user "$$(id -u):$$(id -g)" -e HOME=/tmp \
		$(IMAGE) sleep infinity
	@echo "container '$(DEBUG_NAME)' up -> docker exec -it $(DEBUG_NAME) bash"

image-debug-down:  ## Stop and remove the debug container
	-docker rm -f $(DEBUG_NAME)
