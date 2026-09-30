# Getting Started

WhyGraph is a rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph). CodeGraph
maps what your code *is* - symbols, callers, callees. WhyGraph adds *why* it exists, drawn from the
history around it.

For each chunk of code, it collects evidence from git and GitHub - commits, blame, pull requests, the
issues those PRs closed - and links it together. Then it exposes that evidence to your AI editor over
MCP, plus an on-demand rationale card (purpose, why, constraints, tradeoffs, risks) that it caches.

You install WhyGraph once, start its portal with `whygraph up`, and add the repos you want it to analyze
from the browser. It speaks MCP, so any editor that does too can use it.

## CodeGraph vs WhyGraph

The two tools stay in their lanes:

| Layer | Answers | Examples |
|---|---|---|
| **CodeGraph** | "What's connected to what?" | callers, callees, symbol resolution, type hierarchy |
| **WhyGraph** | "Why does this exist, and when did it change?" | evidence, rationale cards, area history |

Run both. WhyGraph reads CodeGraph's index to resolve a symbol to a file and line range, then layers
its own history on top.

## Prerequisites

You don't need all of these - most are optional, and the phases that depend on them skip cleanly when
they're missing.

- **Docker** - the [container install](installation.md) is the way to run the portal, and needs nothing
  else on your host: no Python, Node, `gh` or CodeGraph.
- **git** - your repo history is the primary evidence source.
- **A GitHub token** - only for GitHub repos, and only if you enable the remote crawl. Enter it in the
  portal. Without it, the GitHub phase is skipped.
- **An LLM API key** (or a local Ollama) - for per-commit descriptions, rationale cards and chat.
  Enter it in the portal. Both phases skip cleanly without one.
- **[uv](https://docs.astral.sh/uv/)** *(optional)* - only for a native install, which provides
  headless `whygraph scan` and no portal.

Ready to install?

<div class="grid cards" markdown>

-   :material-download:{ .lg .middle } __Installation__

    ---

    Docker, PyPI, GitHub, or a local checkout - pick the path that fits.

    [:octicons-arrow-right-24: Install WhyGraph](installation.md)

-   :material-rocket-launch:{ .lg .middle } __Quickstart__

    ---

    Install, `whygraph up`, open the browser - the happy path.

    [:octicons-arrow-right-24: Quickstart](quickstart.md)

-   :material-monitor-dashboard:{ .lg .middle } __The portal__

    ---

    One local server for all your projects: the Explorer, a chat assistant, and an MCP endpoint per
    project.

    [:octicons-arrow-right-24: The portal](../portal/index.md)

</div>
