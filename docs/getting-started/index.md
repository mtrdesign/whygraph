# Getting Started

WhyGraph is a rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph). CodeGraph
maps what your code *is* — symbols, callers, callees. WhyGraph adds *why* it exists, drawn from the
history around it.

For each chunk of code, it collects evidence from git and GitHub — commits, blame, pull requests, the
issues those PRs closed — and links it together. Then it exposes that evidence to your AI editor over
MCP, plus an on-demand rationale card (purpose, why, constraints, tradeoffs, risks) that it caches.

You install WhyGraph once, then wire it into each repo you want it to analyze. It speaks MCP, so any
editor that does too can use it.

## CodeGraph vs WhyGraph

The two tools stay in their lanes:

| Layer | Answers | Examples |
|---|---|---|
| **CodeGraph** | "What's connected to what?" | callers, callees, symbol resolution, type hierarchy |
| **WhyGraph** | "Why does this exist, and when did it change?" | evidence, rationale cards, area history |

Run both. WhyGraph reads CodeGraph's index to resolve a symbol to a file and line range, then layers
its own history on top.

## Prerequisites

You don't need all of these — most are optional, and the phases that depend on them skip cleanly when
they're missing.

- **[uv](https://docs.astral.sh/uv/)** *or* **Docker** — uv for a native install, or Docker alone for
  the [container install](installation.md). With Docker, you need nothing else on your host.
- **git** — your repo history is the primary evidence source.
- **Docker** *(native installs only)* — when no `codegraph` binary is on `PATH`, `whygraph scan` runs
  CodeGraph inside the WhyGraph image to index the repo.
- **[`gh` CLI](https://cli.github.com/)**, authenticated — only for GitHub repos, and only if you
  enable the remote crawl. Without it, the GitHub phase is skipped.
- **`claude` CLI** *or* an LLM API key — for per-commit descriptions and rationale cards. Both phases
  skip cleanly if neither is available.

Ready to install?

<div class="grid cards" markdown>

-   :material-download:{ .lg .middle } __Installation__

    ---

    Docker, PyPI, GitHub, or a local checkout — pick the path that fits.

    [:octicons-arrow-right-24: Install WhyGraph](installation.md)

-   :material-rocket-launch:{ .lg .middle } __Quickstart__

    ---

    Init, scan, wire an editor — the happy path in four commands.

    [:octicons-arrow-right-24: Quickstart](quickstart.md)

</div>
