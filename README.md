# whygraph

Rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph): explains *why* code exists, not just what it does.

For each chunk of code, WhyGraph collects evidence from git history and GitHub - commits, blame, PRs, the issues those PRs closed - then serves it to AI editors over MCP, plus an on-demand rationale card (purpose, why, constraints, tradeoffs, risks) with a persistent cache.

> **📖 Full documentation → <https://mtrdesign.github.io/whygraph/>**
>
> Installation, configuration, the CLI and MCP reference, the Docker delivery, and the service model all live there. This README is just the elevator pitch.

## Quickstart

Install WhyGraph once, then from the repo you want to analyze:

```bash
whygraph init                 # bootstrap the WhyGraph DB + write config
whygraph scan                 # crawl history + refresh CodeGraph + LLM descriptions
whygraph init --agent claude  # wire the MCP server into your editor
whygraph-mcp                  # sanity-check the server (Ctrl-C to exit)
whygraph serve                # browse the graph, evidence + rationale in a local web panel
```

The only-Docker install needs nothing but Docker on the host — one command pulls the image and
installs the shims from inside it (pin a version with the image tag):

```bash
docker run --rm ghcr.io/mtrdesign/whygraph install | sh
```

See the [Getting Started guide](https://mtrdesign.github.io/whygraph/getting-started/) for every install path and the [Quickstart](https://mtrdesign.github.io/whygraph/getting-started/quickstart/) for the walkthrough.

## Develop

```bash
uv sync                       # bootstrap .venv and install deps
uv run pytest                 # full test suite
uv run whygraph version       # CLI sanity check
uv run whygraph-mcp           # launch MCP server on stdio
make docs                     # serve the documentation site locally
```

A `Makefile` wraps the common dev tasks; run `make` to list them. See [`CLAUDE.md`](CLAUDE.md) for the architecture and conventions.
