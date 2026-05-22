# WhyGraph dev tasks. Run `make` (or `make help`) to list targets.
#
# If `uv` or `npx` fail with `UnknownIssuer` SSL errors off-VPN, run e.g.
# `SSL_CERT_FILE= make sync` — make propagates the empty var into the recipe.

.DEFAULT_GOAL := help

# `make inspect REPO=/path/to/repo` points the MCP Inspector at another
# checkout's databases. Defaults to this repo.
REPO ?= .

.PHONY: help sync test scan db db-down inspect

help:  ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  %-10s %s\n", $$1, $$2}'

sync:  ## Install / refresh the uv environment
	uv sync

test:  ## Run the test suite
	uv run pytest

scan:  ## Re-scan this repo so WhyGraph is tested against itself
	uv run whygraph scan

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
