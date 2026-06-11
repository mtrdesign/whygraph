---
hide:
  - navigation
  - toc
---

# WhyGraph

<p style="font-size: 1.6rem; font-weight: 300; margin: 0 0 .4rem;">
Explains <em>why</em> code exists, not just what it does.
</p>

A rationale layer over [CodeGraph](https://github.com/colbymchenry/codegraph). It mines your git
history and GitHub for the story behind each line - the commits, pull requests, and issues that put
it there - and serves that story to any AI editor over MCP.

[Get started](getting-started/quickstart.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/mtrdesign/whygraph){ .md-button }

---

<div class="grid cards" markdown>

-   :material-history:{ .lg .middle } __Evidence from your history__

    ---

    For any chunk of code, WhyGraph pulls the commits that touched it, the blame behind each line,
    the PRs that merged it, and the issues those PRs closed - already linked together.

    [:octicons-arrow-right-24: Concepts](guide/concepts.md)

-   :material-card-text-outline:{ .lg .middle } __Rationale cards__

    ---

    Ask why a symbol exists and get a structured card: purpose, why, constraints, tradeoffs, and
    risks. Each card is cached, so the second lookup is instant.

    [:octicons-arrow-right-24: Using WhyGraph](guide/mcp-usage.md)

-   :material-connection:{ .lg .middle } __MCP-native__

    ---

    `whygraph-mcp` is a standard MCP server over stdio. Claude Code, Cursor, VS Code, Codex - any
    editor that speaks MCP can call it. One command wires each project.

    [:octicons-arrow-right-24: Wire your editor](guide/editors.md)

-   :material-docker:{ .lg .middle } __Only Docker required__

    ---

    No Python, Node, `gh`, or CodeGraph on your host. A tiny shim runs everything inside one image,
    ephemeral per command. Install, init, scan - done.

    [:octicons-arrow-right-24: Run with Docker](deploy/docker.md)

-   :material-graph-outline:{ .lg .middle } __Composes with CodeGraph__

    ---

    CodeGraph answers "what's connected to what". WhyGraph answers "why it exists and when it
    changed". Run both; each stays focused on its own job.

    [:octicons-arrow-right-24: Getting started](getting-started/index.md)

-   :material-server-network:{ .lg .middle } __Git analysis as a service__

    ---

    The MCP server isn't just for editors. Real applications can connect to it for git-based
    analysis of a target repo, reading the same cached data your scan writes.

    [:octicons-arrow-right-24: WhyGraph as a service](deploy/service.md)

</div>

## Who it's for

You're dropping into an unfamiliar codebase, or editing code you wrote months ago and no longer
remember. The *what* is in front of you; the *why* is buried in history. WhyGraph surfaces that why
right where your AI assistant works, so an edit respects the original intent instead of rediscovering
it the hard way.

Ready? [Start with the Quickstart.](getting-started/quickstart.md)
