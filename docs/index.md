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
it there - and serves that story to any AI editor over MCP, or to you in the WhyGraph portal.

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

    Every project gets a standard HTTP MCP endpoint on the portal. Claude Code, Cursor, VS Code, Codex -
    any editor that speaks MCP can call it, and the portal wires each one for you.

    [:octicons-arrow-right-24: Wire your editor](guide/editors.md)

-   :material-docker:{ .lg .middle } __Only Docker required__

    ---

    No Python, Node, `gh`, or CodeGraph on your host. A tiny shim runs everything inside one image.
    Install, `whygraph up`, open the browser - done.

    [:octicons-arrow-right-24: Run with Docker](deploy/docker.md)

-   :material-monitor-dashboard:{ .lg .middle } __The portal__

    ---

    `whygraph up` starts one local server for all your projects: an Explorer over the code graph with
    rationale and evidence side by side, plus a chat assistant that answers questions by calling the
    same tools.

    [:octicons-arrow-right-24: The portal](portal/index.md)

-   :material-graph-outline:{ .lg .middle } __Composes with CodeGraph__

    ---

    CodeGraph answers "what's connected to what". WhyGraph answers "why it exists and when it
    changed". Run both; each stays focused on its own job.

    [:octicons-arrow-right-24: Getting started](getting-started/index.md)

-   :material-server-network:{ .lg .middle } __Git analysis as a service__

    ---

    The MCP endpoints aren't just for editors. Real applications can connect to them for git-based
    analysis of a target repo, reading the same cached data your scans write.

    [:octicons-arrow-right-24: WhyGraph as a service](deploy/service.md)

</div>

## Who it's for

You're dropping into an unfamiliar codebase, or editing code you wrote months ago and no longer
remember. The *what* is in front of you; the *why* is buried in history. WhyGraph surfaces that why
right where your AI assistant works, so an edit respects the original intent instead of rediscovering
it the hard way.

Ready? [Start with the Quickstart.](getting-started/quickstart.md)
