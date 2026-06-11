# WhyGraph dev tasks. Run `make` (or `make help`) to list targets.

.DEFAULT_GOAL := help

# `make inspect REPO=/path/to/repo` points the MCP Inspector at another
# checkout's databases. Defaults to this repo.
REPO ?= .

# Local tag for the dev image built by `make image`. Override to test an
# alternate tag, e.g. `make image IMAGE=whygraph:wip`.
IMAGE ?= whygraph:dev

# Name of the long-running container started by `make image-debug`.
DEBUG_NAME ?= whygraph-debug

.PHONY: help sync test scan docs docs-build db db-down inspect image image-test image-inspect image-debug image-debug-down

help:  ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  %-10s %s\n", $$1, $$2}'

sync:  ## Install / refresh the uv environment
	uv sync

test:  ## Run the test suite
	uv run pytest

scan:  ## Re-scan this repo so WhyGraph is tested against itself
	uv run whygraph scan

docs:  ## Serve the docs site locally with live reload (social cards skipped — no Cairo needed)
	uv run mkdocs serve

docs-build:  ## Build the static docs site into ./site (strict; cards skipped unless CI=true)
	uv run mkdocs build --strict

db:  ## Start the DBGate database viewer (http://localhost:8081)
	@test -f docker-compose.yml || { echo "error: docker-compose.yml missing - run: cp docker-compose.example.yml docker-compose.yml"; exit 1; }
	@test -f .whygraph/whygraph.db || echo "warning: .whygraph/whygraph.db missing - run 'make scan' first"
	@test -f .codegraph/codegraph.db || echo "warning: .codegraph/codegraph.db missing - run 'whygraph init' first"
	docker compose up -d
	@echo "DBGate -> http://localhost:8081  (WhyGraph + CodeGraph in the sidebar)"

db-down:  ## Stop the DBGate database viewer
	docker compose down

inspect:  ## MCP Inspector vs whygraph-mcp (REPO=/path/to/repo targets another checkout)
	@node -e 'process.exit(+process.versions.node.split(".")[0]>=20?0:1)' 2>/dev/null || { echo "error: MCP Inspector needs Node >= 20 (have $$(node -v 2>/dev/null || echo none)) - try 'nvm use 22'"; exit 1; }
	npx @modelcontextprotocol/inspector uv run --directory $(REPO) --project $(CURDIR) whygraph-mcp

image:  ## Build the WhyGraph Docker image locally (override tag: IMAGE=...)
	docker build -f docker/whygraph/Dockerfile -t $(IMAGE) .

image-test: image  ## Build then smoke-test the image (CLI + bundled binaries)
	docker run --rm $(IMAGE) whygraph version
	docker run --rm $(IMAGE) sh -c 'for b in git gh node codegraph whygraph whygraph-mcp; do command -v "$$b" || { echo "missing: $$b" >&2; exit 1; }; done'
	@echo "image smoke test OK -> $(IMAGE)"

image-inspect: image  ## MCP Inspector vs the containerized whygraph-mcp
	@node -e 'process.exit(+process.versions.node.split(".")[0]>=20?0:1)' 2>/dev/null || { echo "error: MCP Inspector needs Node >= 20 (have $$(node -v 2>/dev/null || echo none)) - try 'nvm use 22'"; exit 1; }
	npx @modelcontextprotocol/inspector \
		docker run --rm -i -v "$(CURDIR):/workspace" -w /workspace $(IMAGE) whygraph-mcp

image-debug: image  ## Build, then run a detached container kept alive for `docker exec` debugging
	-docker rm -f $(DEBUG_NAME) 2>/dev/null || true
	docker run -d --name $(DEBUG_NAME) \
		-v "$(CURDIR):/workspace" -w /workspace \
		--user "$$(id -u):$$(id -g)" -e HOME=/tmp \
		$(IMAGE) sleep infinity
	@echo "container '$(DEBUG_NAME)' up -> docker exec -it $(DEBUG_NAME) bash"

image-debug-down:  ## Stop and remove the debug container
	-docker rm -f $(DEBUG_NAME)
