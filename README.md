# whygraph

Rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph): explains *why* code exists, not just what it does.

For each chunk of code, WhyGraph collects evidence from git history and GitHub - commits, blame, PRs, the issues those PRs closed - then serves it to AI editors over MCP, plus an on-demand rationale card (purpose, why, constraints, tradeoffs, risks) with a persistent cache.

The **WhyGraph portal** puts the same data in a local web panel: one server for all your projects, with an **Explorer** over the code graph and a **Chat assistant** that answers questions about the repo by calling WhyGraph's own tools. Each project also gets an HTTP MCP endpoint for your editor.

> **📖 Full documentation → <https://mtrdesign.github.io/whygraph/>**
>
> Installation, configuration, the CLI and MCP reference, the Docker delivery, and the service model all live there. This README is just the elevator pitch.

## Quickstart

Install WhyGraph once, start the portal, and add your repos from the browser:

```bash
whygraph up --add-folder ~/Work   # start the portal, sharing the folder that holds your repos
# open http://127.0.0.1:8765, then add a project: configure, initialize, first scan
```

The portal initializes each repo, connects your agents over HTTP MCP, runs every scan, and keeps
projects fresh from git hooks. Enter API keys and tokens under Settings - variables in your shell do not
reach it.

The only-Docker install needs nothing but Docker on the host - one command pulls the image and
installs the shims from inside it. The tag in the URL is the version you get:

```bash
curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v2.0.0/scripts/install.sh | sh
```

See the [Getting Started guide](https://mtrdesign.github.io/whygraph/getting-started/) for every install path and the [Quickstart](https://mtrdesign.github.io/whygraph/getting-started/quickstart/) for the walkthrough.

## Develop

```bash
uv sync                       # bootstrap .venv and install deps
uv run pytest                 # full test suite
uv run whygraph version       # CLI sanity check
make dev-local                # develop natively: portal (:8777) + Vite HMR (:5173); open :5173
make dev-docker               # the same inside the image, through the real shim
make prod                     # the image exactly as released, through the shim
make check                    # everything CI runs, plus e2e and the release smoke test
```

A `Makefile` wraps the common dev tasks; run `make` to list them, and see [Developing WhyGraph](https://mtrdesign.github.io/whygraph/guide/developing/). See [`CLAUDE.md`](CLAUDE.md) for the architecture and conventions.
